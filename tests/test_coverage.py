import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import free_port, portal_for, small_fixture, write_project
from releaseharbor.catalog import PREVIEW, STRICT, publish_views, reconcile
from releaseharbor.clock import Clock
from releaseharbor.config import load_config
from releaseharbor.db import connect
from releaseharbor.discovery import Discovery
from releaseharbor.httpclient import SafeHttpClient
from releaseharbor.ledger import Ledger
from releaseharbor.queries import query_artifact, query_version, recheck_declaration
from releaseharbor.security import UrlPolicy


class DeclarationTests(unittest.TestCase):
    """Discovery-level coverage declarations on the synthetic A/B portal."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(
            write_project(self.root, free_port(), fixture_obj=small_fixture())
        )
        self.portal = portal_for(self.config).start()
        self.events = []
        self.client = SafeHttpClient(UrlPolicy(self.portal.base_url))

    def tearDown(self):
        self.portal.stop()
        self.temp.cleanup()

    def discover(self, config=None, on_event=None):
        sink = on_event or (lambda code, subject, detail: self.events.append(code))
        discovery = Discovery(self.client, config or self.config, on_event=sink)
        result = discovery.discover()
        discovery.verify_stability()
        return discovery, result

    def persist(self, run_id, result):
        connection = connect(self.config.download.db_path)
        ledger = Ledger(connection, Clock())
        for evidence in result.evidence:
            ledger.record_page_evidence(run_id, evidence)
        for declaration in result.declarations:
            ledger.record_declaration(run_id, declaration)
        views = publish_views(connection, ledger, run_id)
        return connection, views

    @staticmethod
    def declaration_of(result, suffix):
        return next(d for d in result.declarations if d.page_url.endswith(suffix))

    def limited(self, **overrides):
        return replace(
            self.config, discovery=replace(self.config.discovery, **overrides)
        )

    def test_full_round_proves_absence(self):
        _, result = self.discover()
        self.assertTrue(all(d.stability == "confirmed" for d in result.declarations))
        self.assertTrue(all(d.complete for d in result.declarations))
        connection, views = self.persist("run-full", result)
        try:
            self.assertEqual(views[STRICT]["status"], "published")
            answer = query_version(connection, STRICT, "alpha", "v9")
            self.assertEqual(answer["state"], "absent_proven")
            self.assertEqual(answer["view_run_id"], "run-full")
            answer = query_artifact(connection, PREVIEW, "alpha", "v1", "ghost.txt")
            self.assertEqual(answer["state"], "absent_proven")
            answer = query_version(connection, PREVIEW, "gamma", "v1")
            self.assertEqual(answer["state"], "absent_proven")
        finally:
            connection.close()

    def test_budget_truncation_is_unknown_not_absent(self):
        _, result = self.discover(self.limited(max_versions=1, max_artifacts=1))
        alpha = self.declaration_of(result, "/datasets/alpha")
        self.assertEqual((alpha.candidates, alpha.accepted), (2, 1))
        self.assertFalse(alpha.complete)
        decisions = {d.ref: d.outcome for d in alpha.decisions}
        self.assertEqual(decisions["v2"], "budget_skipped")
        v1 = self.declaration_of(result, "/versions/v1")
        self.assertFalse(v1.complete)
        connection, views = self.persist("run-budget", result)
        try:
            self.assertEqual(views[STRICT]["status"], "withheld")
            self.assertEqual(views[PREVIEW]["status"], "published")
            answer = query_version(connection, PREVIEW, "alpha", "v2")
            self.assertEqual((answer["state"], answer["reason"]), ("unknown", "budget_skipped"))
            answer = query_version(connection, PREVIEW, "alpha", "v1")
            self.assertEqual(answer["state"], "present")
            answer = query_artifact(connection, PREVIEW, "alpha", "v1", "a-two.txt")
            self.assertEqual((answer["state"], answer["reason"]), ("unknown", "budget_skipped"))
            answer = query_artifact(connection, PREVIEW, "alpha", "v1", "never.txt")
            self.assertEqual(answer["state"], "unknown")
            answer = query_version(connection, STRICT, "alpha", "v1")
            self.assertEqual((answer["state"], answer["reason"]), ("unknown", "no_directory"))
        finally:
            connection.close()

    def test_channel_filtering_is_reported_not_absent(self):
        self.portal.stop()
        self.temp.cleanup()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = load_config(
            write_project(
                self.root,
                free_port(),
                fixture_obj=small_fixture(alpha_v2_channel="nightly"),
                channels=["stable"],
            )
        )
        self.portal = portal_for(self.config).start()
        self.client = SafeHttpClient(UrlPolicy(self.portal.base_url))
        _, result = self.discover()
        connection, views = self.persist("run-channel", result)
        try:
            answer = query_version(connection, PREVIEW, "alpha", "v2")
            self.assertEqual((answer["state"], answer["reason"]), ("filtered", "channel_filtered"))
            self.assertEqual(views[STRICT]["status"], "published")
            self.assertEqual(views[STRICT]["datasets"]["alpha"]["versions"], ["v1"])
            self.assertEqual(len(views[PREVIEW]["filtered"]), 1)
            answer = query_version(connection, STRICT, "alpha", "v2")
            self.assertEqual(answer["state"], "filtered")
        finally:
            connection.close()

    def test_recheck_declaration_against_retained_evidence(self):
        _, result = self.discover()
        connection, _views = self.persist("run-recheck", result)
        try:
            for declaration in result.declarations:
                outcome = recheck_declaration(connection, "run-recheck", declaration.page_url)
                self.assertTrue(outcome["ok"], outcome)
            connection.execute(
                "UPDATE page_evidence SET content_digest = 'tampered' "
                "WHERE run_id = 'run-recheck' AND fetch_seq = 1"
            )
            connection.commit()
            home = next(d for d in result.declarations if d.level == "portal")
            outcome = recheck_declaration(connection, "run-recheck", home.page_url)
            self.assertFalse(outcome["ok"])
        finally:
            connection.close()

    def test_reconcile_detects_broken_parent_child_chains(self):
        portal = {
            "level": "portal",
            "page_url": "http://x/",
            "parent_url": None,
            "content_digest": "d0",
            "children": {"http://x/a": "d1"},
            "decisions": [{"ref": "a", "url": "http://x/a", "outcome": "accepted", "reason": ""}],
        }
        child = {
            "level": "dataset",
            "page_url": "http://x/a",
            "parent_url": "http://x/",
            "content_digest": "d2",
            "children": {},
            "decisions": [],
        }
        violations = reconcile([dict(portal), dict(child)])
        self.assertTrue(any("digest mismatch" in item for item in violations))
        orphan = dict(child, page_url="http://x/b", parent_url="http://x/missing")
        violations = reconcile([dict(portal), orphan])
        self.assertTrue(any("lacks its parent" in item for item in violations))
        violations = reconcile([dict(portal)])
        self.assertTrue(any("lacks a declaration" in item for item in violations))
