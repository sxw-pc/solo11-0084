"""URL/origin rules, config contract selection, and redirection regressions."""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import EXTERNAL_V4, event_sink, free_port, portal_for, write_project
from pinning_support import aio
from releaseharbor.config import load_config
from releaseharbor.errors import ConfigError, SecurityBoundaryError
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.security import (
    AddressContract,
    UrlPolicy,
    is_loopback_host,
    numeric_loopback,
    resolve_bind_address,
)


class ContractRulesTests(unittest.TestCase):
    def test_numeric_loopback_variants(self):
        self.assertEqual(numeric_loopback("127.0.0.1"), "127.0.0.1")
        self.assertEqual(numeric_loopback("::1"), "::1")
        self.assertIsNone(numeric_loopback("127.0.0.2"))
        self.assertIsNone(numeric_loopback("localhost"))
        self.assertIsNone(numeric_loopback("::1%lo"))
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("LOCALHOST."))

    def test_literal_contract_allows_numeric_loopback_with_exact_port(self):
        policy = UrlPolicy("http://127.0.0.1:18765")
        target = policy.validate("http://127.0.0.1:18765/datasets/a?x=1")
        self.assertEqual(target.host_header, "127.0.0.1:18765")
        for url in (
            "http://localhost:18765/",
            "http://127.0.0.2:18765/",
            "http://127.0.0.1:18766/",
            "http://user:pass@127.0.0.1:18765/",
            "https://127.0.0.1:18765/",
            "http://127.0.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(SecurityBoundaryError):
                policy.validate(url)

    def test_ipv6_literal_contract(self):
        policy = UrlPolicy("http://[::1]:18765")
        target = policy.validate("http://[::1]:18765/datasets/a")
        self.assertEqual(target.host, "::1")
        self.assertEqual(target.host_header, "[::1]:18765")
        for url in (
            "http://[::2]:18765/",
            "http://127.0.0.1:18765/",
            "http://[::1%lo]:18765/",
            "http://user@[::1]:18765/",
        ):
            with self.subTest(url=url), self.assertRaises(SecurityBoundaryError):
                policy.validate(url)

    def test_pinned_name_requires_localhost_or_literal_origin(self):
        with self.assertRaises(SecurityBoundaryError):
            UrlPolicy("http://example.invalid:18765", contract="pinned_name")
        with self.assertRaises(SecurityBoundaryError):
            UrlPolicy("http://localhost:18765", contract="bogus")

    def test_methods_are_limited_to_get_and_head(self):
        client = SafeHttpClient(UrlPolicy("http://127.0.0.1:18765"))
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                client.open(method, "http://127.0.0.1:18765/")

    def test_bind_host_resolution_never_falls_back_to_a_name(self):
        self.assertEqual(resolve_bind_address("127.0.0.1"), "127.0.0.1")
        self.assertEqual(resolve_bind_address("::1"), "::1")
        self.assertEqual(resolve_bind_address("localhost"), "127.0.0.1")
        with self.assertRaises(SecurityBoundaryError):
            resolve_bind_address("localhost", lambda host, port, type=None: aio(0, EXTERNAL_V4))
        with mock.patch.object(
            socket, "getaddrinfo", lambda host, port, type=None: aio(0, EXTERNAL_V4)
        ):
            with self.assertRaises(SecurityBoundaryError):
                resolve_bind_address("localhost")


class ConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_literal_contract_config_rejects_localhost(self):
        path = write_project(self.root, free_port(), host="localhost", contract="literal")
        with self.assertRaises(ConfigError) as caught:
            load_config(path)
        self.assertIn("localhost", str(caught.exception))

    def test_unknown_contract_is_rejected(self):
        path = write_project(self.root, free_port(), contract="wildcard")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_pinned_name_config_loads(self):
        path = write_project(self.root, free_port(), host="localhost", contract="pinned_name")
        config = load_config(path)
        self.assertEqual(config.portal.address_contract, AddressContract.PINNED_NAME)
        self.assertEqual(config.portal.bind_host, "localhost")


class RedirectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.portal = portal_for(self.config).start()

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_redirect_targets_are_blocked_not_followed(self):
        # A redirect to any other origin is validated as a fresh target and
        # refused before a socket exists; the client has no follow mode.
        events, sink = event_sink()
        client = SafeHttpClient(UrlPolicy(self.portal.base_url), on_blocked=sink)
        with self.assertRaises(SecurityBoundaryError):
            client.open("GET", f"http://{EXTERNAL_V4}:{self.portal.port}/")
        self.assertEqual([event["code"] for event in events], ["target.blocked"])
        with self.assertRaises(SecurityBoundaryError):
            client.open("GET", f"http://127.0.0.1:{self.portal.port + 1}/")
        self.assertEqual([event["code"] for event in events], ["target.blocked"] * 2)


if __name__ == "__main__":
    unittest.main()
