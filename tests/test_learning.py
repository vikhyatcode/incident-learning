import copy
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from scripts import learning


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "id": "test", "title": "Provider degradation", "url": "https://example.com/a",
            "source_updated_at": None, "evidence": [{"text": "Degraded then recovered"}],
            "source_id": "github", "last_seen": learning.p.stamp(learning.datetime.now(learning.timezone.utc)),
        }
        self.collection = {"sources": [{"id": "github", "status": "ok",
                                       "last_success": self.record["last_seen"]}]}
        self.assessment = {
            "method": "agent-source-review", "model": "gpt-6.1-sol",
            "analyzed_at": "2026-11-17T10:00:00Z",
            "record_hash": learning.record_hash(self.record),
            "sources": [{"url": self.record["url"], "kind": "record",
                         "sha256": learning.record_hash(self.record)}],
            "dimensions": {key: {"score": 3, "rationale": "Supported specific reasoning."}
                           for key in learning.WEIGHTS},
            "why": "Study the stated failure boundary.",
            "learn": "Test recovery against downstream backlog.",
            "diagram": {
                "normal": "The provider normally serves requests.",
                "root_cause": "Cause not published.",
                "contributors": "Not established.",
                "symptoms": "Degraded requests.",
                "steps": [
                    {"stage": "normal", "label": "Normal requests", "detail": "Provider serves requests.", "source": 0, "state": "reported"},
                    {"stage": "trigger", "label": "Cause not published", "detail": "Trigger and mechanism are unknown.", "source": 0, "state": "unknown"},
                    {"stage": "impact", "label": "Degradation", "detail": "Provider reported degraded operation.", "source": 0, "state": "reported"},
                    {"stage": "recovery", "label": "Recovery", "detail": "Provider reported recovery.", "source": 0, "state": "reported"},
                ],
            },
        }

    def test_scoring_rejects_bool_out_of_range_missing_rationale_and_bad_sources(self):
        self.assertEqual(learning.score(learning.validate(self.assessment)), 75)
        for value in (True, -1, 5, "3"):
            bad = copy.deepcopy(self.assessment)
            bad["dimensions"]["detail"]["score"] = value
            with self.assertRaises(ValueError):
                learning.validate(bad)
        for field in ("rationale",):
            bad = copy.deepcopy(self.assessment)
            bad["dimensions"]["detail"][field] = ""
            with self.assertRaises(ValueError):
                learning.validate(bad)
        bad = copy.deepcopy(self.assessment)
        bad["diagram"]["steps"][0]["source"] = 4
        with self.assertRaises(ValueError):
            learning.validate(bad)

    def test_freshness_changes_failures_and_unassessed(self):
        self.assertEqual(learning.attach(self.record, self.assessment, {}, collection=self.collection)["analysis_status"], "assessed")
        changed = dict(self.record, title="New scope")
        stale = learning.attach(changed, self.assessment, {})
        self.assertEqual(stale["analysis_status"], "stale")
        self.assertNotIn("learning_score", stale)
        self.assertEqual(learning.attach(self.record, None, {})["analysis_status"], "unassessed")
        a = copy.deepcopy(self.assessment)
        a["sources"][0]["kind"] = "article"
        checks = {"sources": {self.record["url"]: {"sha256": "0" * 64, "status": "ok"}}}
        self.assertEqual(learning.attach(self.record, a, checks)["analysis_status"], "stale")
        checks["sources"][self.record["url"]] = {"status": "error", "error": "HTTP 403"}
        self.assertEqual(learning.attach(self.record, a, checks)["analysis_status"], "stale")

    def test_judgments_do_not_reward_sparse_or_irrelevant_reports(self):
        a = copy.deepcopy(self.assessment)
        self.assertEqual(learning.judgment(a), "Learn now")
        a["dimensions"]["detail"]["score"] = 1
        self.assertEqual(learning.judgment(a), "Monitor for details")
        a["dimensions"]["transferability"]["score"] = 1
        self.assertEqual(learning.judgment(a), "Low learning value")

    def test_advisory_survives_refresh_and_merges_reviewed_aliases_only(self):
        advisory = dict(self.record, id="official", aliases=["https://news.example.com/same"])
        duplicate = dict(self.record, id="news", url="https://news.example.com/same")
        unrelated = dict(self.record, id="different", url="https://example.com/other")
        merged = learning.merge_advisories([duplicate, unrelated], [advisory])
        self.assertEqual([r["id"] for r in merged], ["different", "official"])
        self.assertEqual(learning.merge_advisories(merged, [advisory]), merged)

    def test_diagram_is_numbered_escaped_and_unknown_link_is_not_a_cause(self):
        a = copy.deepcopy(self.assessment)
        a["diagram"]["steps"][1]["label"] = '<img src=x onerror="bad">'
        result = learning.render_diagram(a)
        self.assertIn("&lt;img", result)
        self.assertNotIn("<img", result)
        self.assertIn("Unknown", result)
        self.assertIn("<ol", result)
        self.assertIn("2.", result)
        bad = copy.deepcopy(self.assessment)
        bad["diagram"]["feedback"] = {"from": 2, "to": 99, "label": "feedback"}
        with self.assertRaises(ValueError):
            learning.validate(bad)

    def test_committed_assessments_validate_and_use_original_record_identity(self):
        root = Path(__file__).resolve().parents[1]
        document = json.loads((root / "data" / "learning-analysis.json").read_text())
        for value in document["entries"].values():
            learning.validate(value)
        changed = dict(self.record, last_seen="a later run", revisions=[{"old": "copy"}])
        self.assertEqual(learning.record_hash(changed), learning.record_hash(self.record))
        overlay = dict(self.record, summary="Rendered educational overlay")
        self.assertEqual(learning.attach(overlay, self.assessment, {}, self.record, self.collection)["analysis_status"], "assessed")

    def test_provider_failure_or_expired_observation_demotes_retained_assessment(self):
        failed = {"sources": [{"id": "github", "status": "error",
                               "last_success": self.record["last_seen"]},
                              {"id": "cloudflare", "status": "ok",
                               "last_success": self.record["last_seen"]}]}
        result = learning.attach(self.record, self.assessment, {}, collection=failed)
        self.assertEqual(result["analysis_status"], "stale")
        self.assertNotIn("learning_score", result)
        old = dict(self.record, last_seen="2020-01-01T00:00:00Z")
        self.assertEqual(learning.attach(old, self.assessment, {}, collection=self.collection)["analysis_status"], "stale")
        self.assertEqual(learning.attach(self.record, self.assessment, {})["analysis_status"], "stale")

    def test_article_check_success_error_age_and_body_changes(self):
        a = copy.deepcopy(self.assessment)
        a["sources"][0].update(kind="article", sha256=learning.e.digest("actual body"))
        with patch.object(learning.e, "article_url", return_value=self.record["url"]):
            checks = learning.check_sources({"entries": {"test": a}}, lambda url: "actual body")
        self.assertEqual(learning.attach(self.record, a, checks)["analysis_status"], "assessed")
        checks["sources"][self.record["url"]]["checked_at"] = "2020-01-01T00:00:00Z"
        self.assertEqual(learning.attach(self.record, a, checks)["analysis_status"], "stale")
        with self.assertRaises(ValueError):
            learning.e.article_url("https://www.notion.com/en-gb/security/advisories/security-incident-involving-a-third-party-ai-vendor?token=untrusted")

    def test_unassessed_provider_sequence_is_safe_and_does_not_invent_cause(self):
        record = dict(self.record, source_id="github", evidence=[{"text": "<script>bad</script>"}])
        summary, diagram = learning.render(record)
        self.assertIn("unassessed", summary)
        self.assertIn("Cause not published", diagram)
        self.assertIn("&lt;script&gt;", diagram)
        self.assertNotIn("<script>", diagram)

    def test_plugin_manifest_roles_and_skill_paths_exist(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "plugin.json").read_text())
        self.assertNotIn("$schema", manifest)
        self.assertEqual(manifest["agents"], "agents/")
        for role in ("discovery", "research", "writer"):
            text = (root / "agents" / f"incident-{role}.agent.md").read_text()
            self.assertTrue(text.startswith("---\n"))
            self.assertIn("description:", text)
            self.assertIn("tools:", text)
            self.assertNotIn("\nmodel:", text)
        self.assertTrue((root / manifest["skills"][0] / "incident-learning-analysis" / "SKILL.md").is_file())

    def test_architecture_options_validate_edges_and_escape_proposed_controls(self):
        a = copy.deepcopy(self.assessment)
        a["architecture"] = {
            "invariant": "Design question: keep spare capacity after redirection.",
            "response": "Detection was reported; prevention below is proposed.",
            "decisions": [
                {"title": "Bound admission", "proposal": "<script>unsafe</script>",
                 "from": 1, "to": 2, "tradeoff": "Reject some work to protect survivors.",
                 "residual_risk": "Does not repair the latent defect."},
                {"title": "Reserve capacity", "proposal": "Keep independent spare capacity.",
                 "from": 0, "to": 1, "tradeoff": "Higher idle cost.",
                 "residual_risk": "Reserves remain finite."}],
            "validation": "Reduce capacity and redirect synthetic load; measure rejection and recovery.",
            "open_questions": "What request mix activates the defect?",
        }
        rendered = learning.render(dict(self.record, analysis_status="assessed",
                                       analysis=a, learning_score=75,
                                       learning_judgment="Learn now"))[1]
        self.assertIn("Architecture choices", rendered)
        self.assertIn("Proposed alternatives", rendered)
        self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("step 2", rendered)
        self.assertIn("step 3", rendered)
        for field, value in (("from", True), ("to", 9)):
            bad = copy.deepcopy(a)
            bad["architecture"]["decisions"][0][field] = value
            with self.assertRaises(ValueError):
                learning.validate(bad)
        bad = copy.deepcopy(a)
        bad["architecture"]["decisions"][0].update({"from": 2, "to": 0})
        with self.assertRaises(ValueError):
            learning.validate(bad)
        bad = copy.deepcopy(a)
        del bad["architecture"]["decisions"][0]["residual_risk"]
        with self.assertRaises(ValueError):
            learning.validate(bad)
        stale = learning.render(dict(self.record, analysis_status="stale", analysis=a))[1]
        self.assertNotIn("Architecture choices", stale)

    def test_score_help_is_shared_and_reachable_from_each_assessed_card(self):
        import tempfile
        root = Path(__file__).resolve().parents[1]
        cases = json.loads((root / "data" / "case-studies.json").read_text())
        assessments = json.loads((root / "data" / "learning-analysis.json").read_text())["entries"]
        for record in cases:
            record.update(analysis_status="assessed", analysis=assessments[record["id"]],
                          learning_score=100, learning_judgment="Learn now")
        with tempfile.TemporaryDirectory() as folder:
            learning.p.build([], cases, {}, Path(folder))
            page = (Path(folder) / "index.html").read_text(encoding="utf-8")
        from html.parser import HTMLParser
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.guides, self.links = 0, 0
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                self.guides += attrs.get("id") == "score-guide"
                self.links += tag == "a" and attrs.get("href") == "#score-guide"
        elements = Elements()
        elements.feed(page)
        self.assertEqual(elements.guides, 1)
        self.assertGreaterEqual(elements.links, len(cases))
        self.assertIn("not a probability", page)
        self.assertIn("high confidence", page)


if __name__ == "__main__":
    unittest.main()
