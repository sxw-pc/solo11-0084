"""Malicious resolution, peer mismatch, and Host-header security tests."""

import socket
import unittest
from unittest import mock

from helpers import (
    EXTERNAL_V4,
    EXTERNAL_V6,
    FIXTURE,
    event_sink,
    free_port,
    load_portal_fixture,
)
from pinning_support import FakeSocket, aio, factory, state
from releaseharbor.errors import SecurityBoundaryError, TransportError
from releaseharbor.httpclient import _PinnedHTTPConnection, SafeHttpClient
from releaseharbor.security import (
    LoopbackConnector,
    PeerMismatchError,
    ResolvedPeer,
    UrlPolicy,
)
from releaseharbor.portal_server import PortalServer


class MixedResolutionTests(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.events, self.sink = event_sink()

    def _client(self, resolver=None, connector=None, **kwargs):
        kwargs.setdefault("on_event", self.sink)
        return SafeHttpClient(
            UrlPolicy(f"http://localhost:{self.port}", contract="pinned_name"),
            resolver=resolver,
            connector=connector,
            **kwargs,
        )

    def test_external_first_in_mixed_resolution_is_never_dialled(self):
        fake = state(fail=True)
        resolver = lambda host, port, type=None: aio(
            port, EXTERNAL_V4, "127.0.0.1", EXTERNAL_V6
        )
        client = self._client(connector=LoopbackConnector(resolver, factory(fake)), timeout=1.0)
        with mock.patch.object(socket, "getaddrinfo", resolver):
            with self.assertRaises(TransportError):
                client.open("GET", f"http://localhost:{self.port}/datasets/d")
        # Exactly one dial, to the numeric loopback address, never to a
        # non-loopback entry even though it was listed first.
        self.assertEqual(fake["dials"], [(socket.AF_INET, ("127.0.0.1", self.port))])
        self.assertEqual(fake["closed"], 1)
        codes = [event["code"] for event in self.events]
        self.assertIn("target.resolution_filtered", codes)
        self.assertIn("target.connect_failed", codes)
        filtered = next(e for e in self.events if e["code"] == "target.resolution_filtered")
        self.assertIn(f"{EXTERNAL_V4}:{self.port}", filtered["detail"]["discarded"])
        self.assertIn(f"[{EXTERNAL_V6}]:{self.port}", filtered["detail"]["discarded"])

    def test_second_resolution_change_cannot_redirect_the_connection(self):
        fake = state()
        calls = {"count": 0}

        def resolver(host, port, type=None):
            calls["count"] += 1
            return aio(port, "127.0.0.1") if calls["count"] == 1 else aio(port, EXTERNAL_V4)

        client = self._client(connector=LoopbackConnector(resolver, factory(fake)))
        with mock.patch.object(socket, "getaddrinfo", resolver):
            try:
                client.open("GET", f"http://localhost:{self.port}/datasets/d")
            except Exception:
                pass  # the fake socket never returns an HTTP response
        # http.client must not resolve 'localhost' a second time: the pinned
        # numeric peer stays 127.0.0.1 even though re-resolution is external.
        self.assertEqual(calls["count"], 1)
        self.assertEqual(fake["dials"], [(socket.AF_INET, ("127.0.0.1", self.port))])

    def test_literal_contract_ignores_a_poisoned_resolver(self):
        fake = state(fail=True)
        calls = {"count": 0}

        def resolver(host, port, type=None):
            calls["count"] += 1
            return aio(port, EXTERNAL_V4, EXTERNAL_V6)

        client = SafeHttpClient(
            UrlPolicy(f"http://127.0.0.1:{self.port}"),
            connector=LoopbackConnector(resolver, factory(fake)),
            on_event=self.sink,
        )
        with mock.patch.object(socket, "getaddrinfo", resolver):
            with self.assertRaises(TransportError):
                client.open("GET", f"http://127.0.0.1:{self.port}/")
        self.assertEqual(calls["count"], 0)
        self.assertEqual(fake["dials"], [(socket.AF_INET, ("127.0.0.1", self.port))])

    def test_all_external_resolution_is_rejected_before_connect(self):
        fake = state()
        resolver = lambda host, port, type=None: aio(port, EXTERNAL_V4, EXTERNAL_V6)
        client = self._client(connector=LoopbackConnector(resolver, factory(fake)))
        with self.assertRaises(SecurityBoundaryError):
            client.open("GET", f"http://localhost:{self.port}/")
        self.assertEqual(fake["dials"], [])
        self.assertEqual(fake["closed"], 0)
        rejected = next(
            e for e in self.events if e["code"] == "target.resolution_rejected"
        )
        self.assertIn(f"{EXTERNAL_V4}:{self.port}", rejected["detail"]["candidates"])

    def test_wrong_peer_is_disconnected_and_audited(self):
        fake = state()
        connector = LoopbackConnector(
            lambda host, port, type=None: aio(port, "127.0.0.1"),
            factory(fake, peer_override=(EXTERNAL_V4, self.port)),
        )
        with self.assertRaises(PeerMismatchError):
            self._client(connector=connector).open("GET", f"http://localhost:{self.port}/")
        self.assertEqual(fake["closed"], 1)
        self.assertIn("target.peer_mismatch", [event["code"] for event in self.events])

    def test_unreadable_peer_is_treated_as_mismatch(self):
        class NoPeerSocket(FakeSocket):
            def getpeername(self):
                raise OSError("peer unavailable")

        fake = state()
        connector = LoopbackConnector(
            lambda host, port, type=None: aio(port, "127.0.0.1"),
            lambda family, socktype: NoPeerSocket(fake, family, socktype),
        )
        with self.assertRaises(PeerMismatchError):
            self._client(connector=connector).open("GET", f"http://localhost:{self.port}/")
        self.assertEqual(fake["closed"], 1)
        self.assertIn("target.peer_mismatch", [event["code"] for event in self.events])

    def test_resolution_failure_is_audited(self):
        def resolver(host, port, type=None):
            raise socket.gaierror("temporary resolution failure")

        client = self._client(connector=LoopbackConnector(resolver, factory(state())))
        with self.assertRaises(TransportError):
            client.open("GET", f"http://localhost:{self.port}/")
        self.assertIn("target.resolution_failed", [event["code"] for event in self.events])

    def test_ipv6_loopback_is_pinned_with_family(self):
        fake = state(fail=True)
        connector = LoopbackConnector(
            lambda host, port, type=None: aio(port, EXTERNAL_V4, "::1"),
            factory(fake, peer_override=("::1", self.port, 0, 0)),
        )
        with self.assertRaises(TransportError):
            self._client(connector=connector).open("GET", f"http://localhost:{self.port}/")
        self.assertEqual(len(fake["dials"]), 1)
        self.assertEqual(fake["dials"][0][0], socket.AF_INET6)
        self.assertEqual(fake["dials"][0][1][0], "::1")

    def test_mixed_families_with_different_ports_do_not_merge_origins(self):
        policy = UrlPolicy(f"http://localhost:{self.port}", contract="pinned_name")
        target = policy.validate(f"http://localhost:{self.port}/datasets/d")
        client = self._client(
            resolver=lambda host, port, type=None: aio(port, "::1", "127.0.0.1")
        )
        peer = client.pin(target)
        self.assertTrue(peer.is_loopback)
        self.assertEqual(peer.port, self.port)
        # Different port / different host family are different origins.
        for url in (
            f"http://localhost:{self.port + 1}/datasets/d",
            f"http://127.0.0.1:{self.port}/datasets/d",
        ):
            with self.subTest(url=url), self.assertRaises(SecurityBoundaryError):
                policy.validate(url)
        v6policy = UrlPolicy(f"http://[::1]:{self.port}")
        with self.assertRaises(SecurityBoundaryError):
            v6policy.validate(f"http://127.0.0.1:{self.port}/datasets/d")


class HostHeaderTests(unittest.TestCase):
    def setUp(self):
        self.port = free_port()

    def test_host_header_matches_configured_portal(self):
        policy = UrlPolicy(f"http://127.0.0.1:{self.port}")
        client = SafeHttpClient(policy)
        target = client.validate(f"http://127.0.0.1:{self.port}/")
        self.assertEqual(target.host_header, f"127.0.0.1:{self.port}")
        self.assertEqual(
            UrlPolicy(f"http://[::1]:{self.port}").validate(f"http://[::1]:{self.port}/in").host_header,
            f"[::1]:{self.port}",
        )

    def test_wrong_host_port_is_rejected_by_portal(self):
        portal = PortalServer(load_portal_fixture(FIXTURE), "127.0.0.1", 0).start()
        try:
            connection = _PinnedHTTPConnection(
                ResolvedPeer(socket.AF_INET, "127.0.0.1", portal.port),
                timeout=3,
                connector=LoopbackConnector(),
            )
            connection.request("GET", "/", headers={"Host": f"127.0.0.1:{portal.port + 1}"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 421)
        finally:
            portal.stop()
