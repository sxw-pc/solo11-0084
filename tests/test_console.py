import http.client
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import free_port, portal_for, write_project
from releaseharbor.config import load_config
from releaseharbor.console import ConsoleServer
from releaseharbor.db import connect_readonly
from releaseharbor.pipeline import run_pipeline


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = load_config(write_project(Path(cls.temp.name), free_port()))
        cls.portal = portal_for(cls.config).start()
        cls.outcome = run_pipeline(cls.config, reset=True)
        cls.server = ConsoleServer(cls.config.download.db_path, 0).start()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.portal.stop()
        cls.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.status, response.read().decode("utf-8"), response.headers

    def test_dashboard_runs_and_artifacts_are_readable(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(self.outcome.run_id, body)
        self.assertIn("read-only", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        status, body, _ = self.get("/artifacts?status=checksum_mismatch")
        self.assertEqual(status, 200)
        self.assertIn("samples.csv", body)
        status, body, _ = self.get("/runs/" + self.outcome.run_id)
        self.assertIn("budget_lines.csv", body)
        self.assertIn("checksum_mismatch", body)

    def test_artifact_detail_exposes_audit_events(self):
        connection = connect_readonly(self.config.download.db_path)
        try:
            uid = connection.execute(
                "SELECT artifact_uid FROM artifacts WHERE filename='samples.csv'"
            ).fetchone()[0]
        finally:
            connection.close()
        body = self.get("/artifact/" + uid)[1]
        self.assertIn("artifact.quarantined", body)
        self.assertIn("0000000000000000000000000000000000000000000000000000000000000000", body)

    def test_head_post_and_non_loopback_host(self):
        request = urllib.request.Request(self.base + "/", method="HEAD")
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertGreater(int(response.headers["Content-Length"]), 0)
            self.assertEqual(response.read(), b"")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(self.base + "/", method="POST"), timeout=3)
        self.assertEqual(caught.exception.code, 405)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=3)
        connection.request("GET", "/", headers={"Host": "example.invalid"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 421)

    def test_database_connection_is_read_only(self):
        connection = connect_readonly(self.config.download.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM events")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
