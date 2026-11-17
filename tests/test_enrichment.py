import copy
import io
import unittest
from unittest.mock import patch

from scripts import enrichment as e


ARTICLE = """<html><nav>Ignore navigation</nav><div class="articleBody">
<p>Customers lost access to the payment service.</p>
<script>sendSecrets()</script><p>A configuration change caused the outage.</p>
<embed><p>The provider rolled back the change and restored access.</p>
<div class="article-callout"><div class="article-body"><p>Sponsored text.</p></div></div></div>
<section>Reader comments that change independently of the article.</section>
<aside><p>Unrelated story.</p></aside></html>"""
EVIDENCE = ("Customers lost access to the payment service. "
            "A configuration change caused the outage. "
            "The provider rolled back the change and restored access.")


def explanation():
    return {
        "summary": "A configuration change interrupted payments; rollback restored access.",
        "what_happened": {"text": "The payment service became unavailable.",
                          "quotes": ["Customers lost access to the payment service."]},
        "impact": {"text": "Customers could not reach the payment service.",
                   "quotes": ["Customers lost access to the payment service."]},
        "known_cause": {"text": "The provider identified a configuration change.",
                        "quotes": ["A configuration change caused the outage."]},
        "response": {"text": "The provider reverted the change and recovered access.",
                     "quotes": ["The provider rolled back the change and restored access."]},
        "cause": "configuration",
        "lessons": [
            "Rehearse reversing configuration independently of a code deployment, because the recovery here depended on reverting configuration.",
            "Alert on the customer payment path rather than relying only on infrastructure health."
        ],
        "questions": ["Can payment configuration be rolled back safely?",
                      "What user-visible signal would detect this failure?"],
    }


class EnrichmentTests(unittest.TestCase):
    def test_article_worker_emits_utf8_on_windows_console_encodings(self):
        output = io.BytesIO()
        console = io.TextIOWrapper(output, encoding="cp1252")
        with patch.object(e.sys, "argv", ["enrichment", "download", "https://example.com"]), \
                patch.object(e, "download_article", return_value="Provider\u2019s update"), \
                patch.object(e.sys, "stdout", console):
            self.assertEqual(e.main(), 0)
        console.flush()
        self.assertEqual(output.getvalue().decode("utf-8").strip(), "Provider\u2019s update")

    def test_extracts_only_article_body_without_scripts_or_surrounding_news(self):
        self.assertEqual(e.extract_article(ARTICLE), EVIDENCE)
        self.assertEqual(e.extract_article(ARTICLE.replace('class="articleBody"', 'class="articlebody clear cf"')),
                         EVIDENCE)
        with self.assertRaises(ValueError):
            e.extract_article("<html><p>No recognized article.</p></html>")

    def test_hash_covers_corrections_beyond_reading_preview_length(self):
        prefix = "<article><p>" + "Original finding. " * 1600
        first = e.extract_article(prefix + "Original scope.</p></article>", "article")
        corrected = e.extract_article(prefix + "Corrected scope.</p></article>", "article")
        self.assertNotEqual(e.digest(first), e.digest(corrected))

    def test_rejects_non_allowlisted_urls_and_redirects(self):
        for url in ("http://www.bleepingcomputer.com/news/security/test/",
                    "https://www.bleepingcomputer.com.evil.test/news/x",
                    "https://127.0.0.1/news/x",
                    "https://user@www.bleepingcomputer.com/news/x",
                    "https://www.bleepingcomputer.com:444/news/x",
                    "https://www.bleepingcomputer.com/news/../private",
                    "https://www.bleepingcomputer.com/news/x?secret=value"):
            with self.assertRaises(ValueError, msg=url):
                e.article_url(url)
        self.assertEqual(e.article_url("https://www.bleepingcomputer.com/news/security/test/"),
                         "https://www.bleepingcomputer.com/news/security/test/")
        with self.assertRaises(ValueError):
            e.ArticleRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test/")

    def test_explanation_requires_exact_evidence_for_facts_and_cause(self):
        validated = e.validate(explanation(), EVIDENCE)
        self.assertEqual(validated["cause"], "configuration")
        for mutate in (
            lambda x: x["impact"].update(quotes=["A million customers lost money."]),
            lambda x: x["known_cause"].update(quotes=[]),
            lambda x: x.update(cause="imaginary"),
            lambda x: x.update(lessons="not a list"),
            lambda x: x.update(summary="<script>run()</script>"),
        ):
            payload = explanation()
            mutate(payload)
            with self.assertRaises(ValueError):
                e.validate(payload, EVIDENCE)
        payload = explanation()
        payload["cause"] = "unknown"
        payload["known_cause"] = {"text": "Not established by the available source evidence.", "quotes": []}
        self.assertEqual(e.validate(payload, EVIDENCE)["cause"], "unknown")

    def test_cache_survives_daily_refresh_but_invalidates_when_evidence_changes(self):
        record = {"id": "test", "title": "Payment outage", "url": "https://example.com",
                  "source_id": "github", "source_updated_at": "2026-11-17T01:00:00Z",
                  "evidence": [{"at": "2026-11-17T01:00:00Z", "text": EVIDENCE}],
                  "summary": "feed", "cause": "unknown"}
        cache = {}
        enriched, health = e.enrich([record], cache, "2026-11-17T02:00:00Z",
                                   lambda r: EVIDENCE, lambda r, evidence: explanation())
        self.assertEqual(enriched[0]["cause"], "configuration")
        self.assertEqual(health["explained"], 1)
        def no_network(*args):
            raise AssertionError("Cache should avoid network and model calls")
        again, _ = e.enrich([record], cache, "2026-11-17T03:00:00Z", no_network, no_network)
        self.assertEqual(again[0]["summary"], enriched[0]["summary"])
        changed = copy.deepcopy(record)
        changed["evidence"][0]["text"] += " A later update arrived."
        failed, health = e.enrich([changed], cache, "2026-11-17T04:00:00Z",
                                 lambda r: EVIDENCE, lambda r, evidence: (_ for _ in ()).throw(OSError("quota")))
        self.assertEqual(failed[0]["cause"], "unknown")
        self.assertEqual(health["status"], "partial")
        self.assertIn("quota", failed[0]["enrichment_error"])

    def test_budget_leaves_explicit_pending_entries_and_continues_after_failure(self):
        records = [{"id": str(i), "title": f"Report {i}", "source_id": "github",
                    "url": "https://example.com", "evidence": [], "cause": "unknown"}
                   for i in range(4)]
        def loader(r):
            if r["id"] == "0":
                raise OSError("source unavailable")
            return EVIDENCE
        enriched, health = e.enrich(records, {}, "2026-11-17T00:00:00Z",
                                   loader, lambda r, evidence: explanation(), budget=2)
        self.assertEqual(health["attempted"], 2)
        self.assertIn("source unavailable", enriched[0]["enrichment_error"])
        self.assertEqual(enriched[1]["cause"], "configuration")
        self.assertIn("budget", enriched[2]["enrichment_error"])

    def test_changed_article_requires_review_even_when_feed_also_updates(self):
        record = {"id": "test", "title": "Payment outage",
                  "url": "https://www.bleepingcomputer.com/news/security/test/",
                  "source_id": "bleepingcomputer", "evidence": [], "cause": "unknown"}
        notes = {"records": {record["url"]: explanation()}, "reviewed_at": "2026-11-17"}
        stored = e.run([record], notes, {}, "first", lambda url: EVIDENCE)
        del stored["entries"]["test"]["explanation_revision"]
        changed = dict(record, title="Updated payment outage")
        failed = e.run([changed], notes, stored, "second",
                       lambda url: EVIDENCE + " The original account is disputed.")
        rendered = e.for_build([changed], notes, failed)[0]
        self.assertIn("needs re-review", rendered["enrichment_error"])
        self.assertEqual(rendered["cause"], "unknown")
        revised = copy.deepcopy(notes)
        revised["reviewed_at_by_url"] = {record["url"]: "2026-11-18"}
        refreshed = e.run([changed], revised, failed, "third",
                          lambda url: EVIDENCE + " The original account is disputed.")
        self.assertEqual(refreshed["health"]["explained"], 1)
        self.assertEqual(e.for_build([changed], revised, refreshed)[0]["cause"], "configuration")

    def test_reviewing_another_report_does_not_authorize_changed_source(self):
        url = "https://www.bleepingcomputer.com/news/security/test/"
        records = [{"id": str(i), "url": url + str(i), "source_id": "bleepingcomputer"}
                   for i in range(2)]
        notes = {"records": {r["url"]: explanation() for r in records}, "reviewed_at": "first",
                 "reviewed_at_by_url": {r["url"]: "first" for r in records}}
        stored = e.run(records, notes, {}, "first", lambda url: EVIDENCE)
        notes["reviewed_at"] = "second"
        notes["reviewed_at_by_url"][records[1]["url"]] = "second"
        result = e.run(records, notes, stored, "second",
                       lambda url: EVIDENCE + " A later correction narrows the claim.")
        self.assertIn("needs re-review", result["errors"]["0"])
        self.assertNotIn("1", result["errors"])

    def test_build_recomputes_health_after_feed_invalidates_cached_explanation(self):
        record = {"id": "test", "title": "Payment outage",
                  "url": "https://www.bleepingcomputer.com/news/security/test/",
                  "source_id": "bleepingcomputer", "evidence": [], "cause": "unknown"}
        notes = {"records": {record["url"]: explanation()}, "reviewed_at": "2026-11-17"}
        stored = e.run([record], notes, {}, "first", lambda url: EVIDENCE)
        changed = dict(record, title="Updated payment outage")
        files = {"incidents.json": [changed], "current-explanations.json": notes,
                 "explanations.json": stored, "collection.json": {}, "case-studies.json": []}
        with patch.object(e.p, "read_json", side_effect=lambda path, default: files.get(path.name, default)), \
                patch.object(e.p, "build") as build, \
                patch.object(e.sys, "argv", ["pipeline", "build"]):
            self.assertEqual(e.p.main(), 0)
        health = build.call_args.args[2]["explanations"]
        self.assertEqual(health["explained"], 0)
        self.assertEqual(health["status"], "partial")
        self.assertEqual(health["checked_at"], "first")

    def test_curated_advisory_is_explained_without_claiming_an_article_check(self):
        record = {"id": "advisory:test", "source_id": "advisory",
                  "url": "https://example.com/advisory", "explanation_status": "reviewed"}
        stored = e.run([record], {"records": {}, "reviewed_at": "2026-11-17"},
                       {}, "now", lambda url: self.fail("No article check was selected"))
        self.assertEqual(stored["health"]["attempted"], 0)
        self.assertEqual(stored["health"]["explained"], 1)
        self.assertEqual(stored["health"]["unreviewed"], 0)
        self.assertEqual(stored["health"]["status"], "ok")
        self.assertEqual(e.for_build([record], {"records": {}}, stored), [record])

    def test_provider_timeline_has_real_operations_and_separates_recovery_from_cause(self):
        record = {"title": "Actions job delays", "source_name": "GitHub Status",
                  "source_id": "github", "status": "resolved",
                  "evidence": [
                      {"at": "2026-11-17T01:00:00Z", "text": "Actions jobs are delayed."},
                      {"at": "2026-11-17T02:00:00Z", "text": "The backlog has cleared."}]}
        result = e.provider_timeline(record)
        self.assertIn("2026-11-17T01:00:00Z", result["what_happened"])
        self.assertIn("2026-11-17T02:00:00Z", result["response"])
        self.assertIn("backlog", " ".join(result["lessons"]).lower())
        self.assertEqual(result["cause"], "unknown")
        self.assertEqual(result["explanation_status"], "timeline")

    def test_build_renders_section_citations_and_visible_enrichment_failure(self):
        from scripts import pipeline as p
        import tempfile
        from pathlib import Path
        record = {"id": "test", "title": "Payment outage", "url": "https://example.com",
                  "source_id": "github", "source_name": "Example", "type": "outage",
                  "historical": False, "published_at": "2026-11-17T01:00:00Z",
                  "source_updated_at": None, "status": "resolved", "cause": "unknown",
                  "summary": "Feed summary", "evidence": []}
        records, health = e.enrich([record], {}, "2026-11-17T02:00:00Z",
                                   lambda r: EVIDENCE, lambda r, ev: explanation())
        records[0]["enrichment_error"] = "<blocked>"
        with tempfile.TemporaryDirectory() as folder:
            p.build(records, [], {"explanations": health}, Path(folder))
            page = (Path(folder) / "index.html").read_text(encoding="utf-8")
        self.assertIn("Source citations by section", page)
        self.assertIn("configuration change caused", page)
        self.assertIn("Explanation update unavailable: &lt;blocked&gt;", page)
        self.assertIn("1 of 1 current reports explained", page)


if __name__ == "__main__":
    unittest.main()
