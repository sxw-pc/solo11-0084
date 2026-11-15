import unittest

from releaseharbor.errors import SecurityBoundaryError
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.security import (
    UrlPolicy,
    is_loopback_host,
    require_loopback_bind,
)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.policy = UrlPolicy("http://127.0.0.1:18765")

    def test_exact_loopback_origin_is_allowed(self):
        target = self.policy.validate("http://127.0.0.1:18765/datasets/a?x=1")
        self.assertEqual(target.request_target, "/datasets/a?x=1")
        self.assertEqual(target.host_header, "127.0.0.1:18765")

    def test_other_hosts_schemes_and_origins_are_refused(self):
        for url in (
            "http://127.0.0.2:18765/",
            "http://example.invalid/",
            "https://127.0.0.1:18765/",
            "http://localhost:18765/",
            "http://user:pass@127.0.0.1:18765/",
        ):
            with self.subTest(url=url), self.assertRaises(SecurityBoundaryError):
                self.policy.validate(url)

    def test_blocked_target_is_audited_before_connection(self):
        events = []
        client = SafeHttpClient(
            self.policy, on_blocked=lambda code, subject, detail: events.append((code, subject))
        )
        with self.assertRaises(SecurityBoundaryError):
            client.open("GET", "http://198.51.100.8:18765/")
        self.assertEqual(events[0][0], "target.blocked")
        self.assertEqual(events[0][1], "http://198.51.100.8:18765/")

    def test_client_rejects_non_read_methods_and_extra_headers(self):
        client = SafeHttpClient(self.policy)
        with self.assertRaises(ValueError):
            client.open("POST", "http://127.0.0.1:18765/")
        with self.assertRaises(ValueError):
            client.open("GET", "http://127.0.0.1:18765/", {"X-Test": "1"})

    def test_bind_host_must_be_named_loopback(self):
        self.assertTrue(is_loopback_host("localhost"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("127.0.0.2"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertEqual(require_loopback_bind("127.0.0.1"), "127.0.0.1")
        with self.assertRaises(SecurityBoundaryError):
            require_loopback_bind("0.0.0.0")


if __name__ == "__main__":
    unittest.main()
