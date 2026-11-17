"""Fake resolver/socket support for loopback peer-pinning tests.

Nothing here ever touches a real network endpoint: dials are recorded in an
in-memory state dictionary and a connected peer is emulated.
"""

from __future__ import annotations

import socket

from helpers import EXTERNAL_V4, EXTERNAL_V6, addr_infos, event_sink  # noqa: F401


def aio(port: int, *addresses: str) -> list[tuple]:
    """getaddrinfo-shaped results for numeric addresses (empty when none)."""
    return addr_infos(port, *addresses) if addresses else []


def state(**overrides) -> dict:
    base = {"dials": [], "peer": None, "closed": 0, "fail": False, "timeout": None}
    base.update(overrides)
    return base


class FakeSocket:
    """Records the exact address handed to ``connect`` and the observed peer."""

    def __init__(self, factory_state, family, socktype, *, peer_override=None):
        self.state = factory_state
        self.family = family
        self.socktype = socktype
        self._peer_override = peer_override

    def settimeout(self, timeout):
        self.state["timeout"] = timeout

    def setsockopt(self, *args):
        self.state["sockopts"] = self.state.get("sockopts", 0) + 1

    def connect(self, sockaddr):
        self.state["dials"].append((self.family, sockaddr))
        if self._peer_override is not None:
            self.state["peer"] = self._peer_override
        elif self.family == socket.AF_INET6:
            self.state["peer"] = (sockaddr[0], sockaddr[1], 0, 0)
        else:
            self.state["peer"] = (sockaddr[0], sockaddr[1])
        if self.state["fail"]:
            raise ConnectionRefusedError("fake connection refused")

    def getpeername(self):
        return self.state["peer"]

    def sendall(self, data):
        self.state["sent"] = self.state.get("sent", 0) + len(data)

    def close(self):
        self.state["closed"] += 1


def factory(fake_state, *, peer_override=None):
    def build(family, socktype):
        return FakeSocket(fake_state, family, socktype, peer_override=peer_override)

    return build
