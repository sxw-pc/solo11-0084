import http.client
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.fixture import load_portal_fixture
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.security import UrlPolicy


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(write_project(Path(self.temp.name), free_port()))
        self.fixture = load_portal_fixture(self.config.portal.fixture)
        self.portal = portal_for(self.config).start()
        self.client = SafeHttpClient(UrlPolicy(self.portal.base_url))

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def test_home_dataset_and_version_pages(self):
        with self.client.open("GET", self.portal.base_url + "/") as response:
            body = response.text()
            self.assertEqual(response.status, 200)
            self.assertIn("dataset-link", body)
        url = self.portal.base_url + "/datasets/civic-budget/versions/2025.1"
        with self.client.open("GET", url) as response:
            self.assertIn("artifact-link", response.text())

    def test_artifact_head_range_and_invalid_range(self):
        url = self.portal.base_url + "/files/civic-budget/2025.1/budget_lines.csv"
        with self.client.open("HEAD", url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_length, 112)
            self.assertEqual(response.headers["accept-ranges"], "bytes")
        with self.client.open("GET", url, {"Range": "bytes=10-19"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read_limited(100)), 10)
            self.assertTrue(response.headers["content-range"].startswith("bytes 10-19/"))
        with self.client.open("GET", url, {"Range": "bytes=9999-"}) as response:
            self.assertEqual(response.status, 416)

    def test_transient_fault_then_success_and_read_only_routes(self):
        url = self.portal.base_url + "/files/civic-budget/2025.1/budget_notes.txt"
        with self.client.open("GET", url) as response:
            self.assertEqual(response.status, 503)
        with self.client.open("GET", url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(len(response.read_limited(1000)), 157)

    def test_traversal_is_not_routed(self):
        url = self.portal.base_url + "/files/civic-budget/2025.1/..%2Fsecret"
        with self.client.open("GET", url) as response:
            self.assertEqual(response.status, 404)

    def test_post_is_rejected(self):
        request = urllib.request.Request(self.portal.base_url + "/", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(caught.exception.headers["Allow"], "GET, HEAD")

    def test_non_loopback_host_header_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.portal.port, timeout=3)
        connection.request("GET", "/", headers={"Host": "example.invalid"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 421)


if __name__ == "__main__":
    unittest.main()
