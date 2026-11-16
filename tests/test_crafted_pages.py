"""Failure, invalid-link, and mid-traversal-change shapes on crafted pages."""

import tempfile
import unittest
from pathlib import Path

from helpers import (
    PageServer,
    dataset_page,
    good_anchor,
    home_page,
    small_fixture,
    version_page,
    write_project,
)
from releaseharbor.catalog import PREVIEW, STRICT, publish_views
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.discovery import Discovery
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.ledger import Ledger
from releaseharbor.queries import query_artifact, query_version
from releaseharbor.security import UrlPolicy


class CraftedPageTests(unittest.TestCase):
    """Hand-crafted synthetic pages for failure and invalid-link shapes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_server(self, pages, on_event=None, **discovery_override):
        server = PageServer(pages).start()
        self.addCleanup(server.stop)
        config = load_config(
            write_project(
                self.root / "proj",
                server.port,
                fixture_obj=small_fixture(),
                discovery_override=discovery_override,
            )
        )
        client = SafeHttpClient(UrlPolicy(server.base_url))
        events = []

        def sink(code, subject, detail):
            events.append(code)
            if on_event is not None:
                on_event(code, subject, detail)

        discovery = Discovery(client, config, on_event=sink)
        result = discovery.discover()
        discovery.verify_stability()
        connection = connect(config.download.db_path)
        ledger = Ledger(connection, Clock())
        for evidence in result.evidence:
            ledger.record_page_evidence("run-crafted", evidence)
        for declaration in result.declarations:
            ledger.record_declaration("run-crafted", declaration)
        views = publish_views(connection, ledger, "run-crafted")
        self.addCleanup(connection.close)
        return connection, result, views, events

    def test_page_change_mid_traversal_rejects_snapshot(self):
        pages = {
            "/": (200, home_page("alpha", "beta")),
            "/datasets/alpha": (200, dataset_page("alpha", "v1", "v2")),
            "/datasets/alpha/versions/v1": (
                200,
                version_page("alpha", "v1", [good_anchor("alpha", "v1", "a-one.txt")]),
            ),
            "/datasets/alpha/versions/v2": (
                200,
                version_page("alpha", "v2", [good_anchor("alpha", "v2", "a-v2.txt")]),
            ),
            "/datasets/beta": (200, dataset_page("beta", "v1")),
            "/datasets/beta/versions/v1": (
                200,
                version_page("beta", "v1", [good_anchor("beta", "v1", "b-v1.txt")]),
            ),
        }
        swapped = []

        def hook(code, subject, detail):
            if code == "page.fetched" and subject.endswith("/datasets/alpha") and not swapped:
                swapped.append(True)
                # The alpha listing grows v3 while traversal is in flight.
                pages["/datasets/alpha"] = (200, dataset_page("alpha", "v1", "v2", "v3"))
                pages["/datasets/alpha/versions/v3"] = (
                    200,
                    version_page("alpha", "v3", [good_anchor("alpha", "v3", "a-v3.txt")]),
                )

        connection, result, views, events = self.run_server(pages, on_event=hook)
        alpha = next(d for d in result.declarations if d.page_url.endswith("/datasets/alpha"))
        self.assertEqual(alpha.stability, "rejected")
        self.assertFalse(alpha.usable)
        self.assertIn("page.changed", events)
        self.assertEqual(len(alpha.children), 2)  # v1/v2 only: v3 is never stitched in
        self.assertNotIn("v3", {d.ref for d in alpha.decisions})
        self.assertEqual(views[STRICT]["status"], "published")
        self.assertEqual(sorted(views[STRICT]["datasets"]), ["beta"])
        reasons = {item["reason"] for item in views[PREVIEW]["unknowns"]}
        self.assertIn("stability_rejected", reasons)
        answer = query_version(connection, PREVIEW, "alpha", "v3")
        self.assertEqual(answer["state"], "unknown")
        answer = query_version(connection, PREVIEW, "alpha", "v1")
        self.assertEqual(answer["state"], "present")
        answer = query_artifact(connection, PREVIEW, "alpha", "v1", "never.txt")
        self.assertEqual(answer["state"], "absent_proven")

    def test_child_page_failure_is_unknown_not_absent(self):
        pages = {
            "/": (200, home_page("alpha", "beta")),
            "/datasets/alpha": (200, dataset_page("alpha", "v1")),
            "/datasets/alpha/versions/v1": (
                200,
                version_page("alpha", "v1", [good_anchor("alpha", "v1", "a.txt")]),
            ),
            "/datasets/beta": (500, "boom"),
        }
        connection, result, views, _events = self.run_server(pages)
        portal = next(d for d in result.declarations if d.level == "portal")
        self.assertFalse(portal.complete)
        decisions = {d.ref: d.outcome for d in portal.decisions}
        self.assertEqual(decisions["beta"], "fetch_failed")
        self.assertEqual(views[STRICT]["status"], "withheld")
        answer = query_version(connection, PREVIEW, "beta", "v1")
        self.assertEqual((answer["state"], answer["reason"]), ("unknown", "fetch_failed"))
        answer = query_version(connection, PREVIEW, "alpha", "v1")
        self.assertEqual(answer["state"], "present")

    def test_invalid_artifact_link_blocks_completeness(self):
        anchors = [
            good_anchor("alpha", "v1", "good.txt"),
            {
                "href": "/files/alpha/v1/bad.txt",
                "name": "bad.txt",
                "size": 3,
                "sha256": "not-a-sha256",
            },
            {
                "href": "http://169.254.169.254/latest/meta-data",
                "name": "evil.txt",
                "size": 3,
                "sha256": "0" * 64,
            },
        ]
        pages = {
            "/": (200, home_page("alpha")),
            "/datasets/alpha": (200, dataset_page("alpha", "v1")),
            "/datasets/alpha/versions/v1": (200, version_page("alpha", "v1", anchors)),
        }
        connection, result, views, _events = self.run_server(pages)
        version = next(d for d in result.declarations if d.level == "version")
        self.assertEqual((version.candidates, version.accepted), (3, 1))
        self.assertFalse(version.complete)
        outcomes = sorted(d.outcome for d in version.decisions)
        self.assertEqual(outcomes, ["accepted", "invalid_link", "invalid_link"])
        self.assertEqual(len(result.blocked), 1)
        self.assertEqual(views[STRICT]["status"], "withheld")
        answer = query_artifact(connection, PREVIEW, "alpha", "v1", "good.txt")
        self.assertEqual(answer["state"], "present")
        answer = query_artifact(connection, PREVIEW, "alpha", "v1", "never-listed.txt")
        self.assertEqual(answer["state"], "unknown")
        self.assertNotEqual(answer["state"], "absent_proven")


if __name__ == "__main__":
    unittest.main()
