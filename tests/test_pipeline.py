import copy
import json
import mimetypes
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import pipeline as p


NOW = datetime(2026, 11, 17, 8, tzinfo=timezone.utc)
STATUS = {
    "incidents": [{
        "id": "abc123", "name": "Major API outage", "impact": "major",
        "status": "resolved", "created_at": "2026-11-16T10:01:00Z",
        "started_at": "2026-11-16T10:00:00Z",
        "updated_at": "2026-11-16T11:00:00Z",
        "shortlink": "https://status.example/incidents/abc123",
        "incident_updates": [
            {"body": "Service has recovered.", "status": "resolved",
             "display_at": "2026-11-16T11:00:00Z"},
            {"body": "<p>Customers cannot access the API.</p>", "status": "investigating",
             "display_at": "2026-11-16T10:01:00Z"},
        ],
    }]
}
RSS = b"""<rss version="2.0"><channel><title>Security news</title>
<item><title>Payment company confirms customer data breach</title>
<link>https://news.example/breach?utm_source=rss</link>
<guid>123</guid><pubDate>Mon, 16 Nov 2026 10:00:00 -0500</pubDate>
<description><![CDATA[<p>The company says hackers stole payment information.</p>]]></description>
</item>
<item><title>Critical flaw could expose millions</title>
<link>https://news.example/flaw</link><pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate>
<description>A patch is available. No exploitation has been observed.</description></item>
<item><title>Researchers simulate ransomware attack on hospitals</title>
<link>https://news.example/simulation</link><pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate>
<description>A proof-of-concept test, not a real incident.</description></item>
</channel></rss>"""


class ParsingTests(unittest.TestCase):
    def test_daily_intake_limit_does_not_prevent_tracked_updates(self):
        body = {"incidents": []}
        for number in range(25):
            incident = copy.deepcopy(STATUS["incidents"][0])
            incident["id"] = f"id{number}"
            body["incidents"].append(incident)
        records, scanned = p.parse_source(p.SOURCES[0], json.dumps(body).encode(), NOW)
        self.assertEqual(len(records), 20)
        self.assertEqual(scanned, 25)
        records, _ = p.parse_source(p.SOURCES[0], json.dumps(body).encode(), NOW,
                                    frozenset({"github:id24"}))
        self.assertEqual(len(records), 21)
        self.assertIn("github:id24", {r["id"] for r in records})

    def test_rss_does_not_invent_an_update_timestamp(self):
        records, _ = p.parse_source(p.SOURCES[2], RSS, NOW)
        self.assertIsNone(records[0]["source_updated_at"])

    def test_status_uses_major_impact_and_separate_dates(self):
        body = copy.deepcopy(STATUS)
        minor = copy.deepcopy(body["incidents"][0])
        minor.update(id="minor", impact="minor")
        body["incidents"].append(minor)
        records, scanned = p.parse_source(p.SOURCES[0], json.dumps(body).encode(), NOW)
        self.assertEqual(scanned, 2)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["incident_date"], "2026-11-16T10:00:00Z")
        self.assertEqual(record["published_at"], "2026-11-16T10:01:00Z")
        self.assertEqual(record["cause"], "unknown")
        self.assertIn("Customers cannot access", record["impact"])
        self.assertNotIn("<p>", record["impact"])
        self.assertIn("recovered", record["response"])

    def test_rss_excludes_vulnerability_catalog_and_simulations(self):
        records, _ = p.parse_source(p.SOURCES[2], RSS, NOW)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "security")
        self.assertIsNone(records[0]["incident_date"])
        self.assertEqual(records[0]["published_at"], "2026-11-16T15:00:00Z")
        self.assertEqual(records[0]["url"], "https://news.example/breach")
        self.assertEqual(records[0]["cause"], "unknown")
        self.assertIn("not yet known", records[0]["known_cause"].lower())

    def test_atom_and_untrusted_markup(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>
        <entry><id>xyz</id><title>Global service outage</title>
        <link href="https://news.example/outage"/><published>2026-11-16T10:00:00Z</published>
        <updated>2026-11-17T00:00:00Z</updated><summary type="html">
        &lt;script&gt;evil()&lt;/script&gt;&lt;p&gt;Worldwide outage disrupted customers.&lt;/p&gt;
        </summary></entry></feed>"""
        records, _ = p.parse_source(p.SOURCES[2], atom, NOW)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "outage")
        self.assertNotIn("evil", records[0]["summary"])
        self.assertEqual(records[0]["source_updated_at"], "2026-11-17T00:00:00Z")

    def test_rejects_bad_schema_links_dates_and_old_entries(self):
        for body in (b"{}", b'{"incidents":"oops"}', b"<html>blocked</html>"):
            with self.assertRaises(ValueError):
                p.parse_source(p.SOURCES[0] if body.startswith(b"{") else p.SOURCES[2], body, NOW)
        for link in ("javascript:alert(1)", "https://user:pass@example.com", "//example.com"):
            self.assertIsNone(p.safe_url(link))
        stale = copy.deepcopy(STATUS)
        stale["incidents"][0]["started_at"] = "2020-01-01T00:00:00Z"
        self.assertEqual(p.parse_source(p.SOURCES[0], json.dumps(stale).encode(), NOW)[0], [])
        malformed = copy.deepcopy(STATUS)
        malformed["incidents"][0]["created_at"] = "not-a-date"
        with self.assertRaises(ValueError):
            p.parse_source(p.SOURCES[0], json.dumps(malformed).encode(), NOW)

    def test_unqualified_news_is_not_assumed_major(self):
        for title, description in [
            ("Small website hacked", "One user's account was breached."),
            ("Ransomware suspect arrested", "The suspect targeted hospitals in past years."),
            ("Company denies data breach", "No customer data was stolen."),
        ]:
            data = RSS.replace(b"Payment company confirms customer data breach", title.encode())
            data = data.replace(b"The company says hackers stole payment information.", description.encode())
            self.assertEqual(p.parse_source(p.SOURCES[2], data, NOW)[0], [])


class CollectionTests(unittest.TestCase):
    def test_malformed_nested_source_fields_are_visible_partial_failures(self):
        archive, health = p.collect([], {}, NOW, self.fetch)
        for field, value in (("incident_updates", [None]), ("updated_at", 42), ("shortlink", 42),
                             ("incident_updates", "not-an-array")):
            broken = copy.deepcopy(STATUS)
            broken["incidents"][0][field] = value
            def partial(source):
                return json.dumps(broken).encode() if source["id"] == "github" else self.fetch(source)
            updated, report = p.collect(archive, health, NOW, partial)
            self.assertEqual(report["status"], "partial")
            self.assertIn("github:abc123", {r["id"] for r in updated})
            self.assertEqual(report["sources"][0]["status"], "error")

    def test_stalled_download_has_wall_clock_deadline_and_other_sources_continue(self):
        import sys
        command = [sys.executable, "-c", "import time; time.sleep(10)"]
        started = time.monotonic()
        def partial(source):
            return p.fetch(source) if source["id"] == "github" else self.fetch(source)
        with patch.object(p, "DOWNLOAD_COMMAND", command), patch.object(p, "FETCH_DEADLINE", 0.2):
            records, report = p.collect([], {}, NOW, partial)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(report["status"], "partial")
        self.assertIn("deadline", report["sources"][0]["error"])
        self.assertTrue(records)

    def fetch(self, source):
        if source["kind"] == "statuspage":
            return json.dumps(STATUS).encode()
        return RSS

    def test_deduplicates_and_updates_with_history(self):
        archive, health = p.collect([], {}, NOW, self.fetch)
        later = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
        def changed(source):
            return self.fetch(source).replace(b"Service has recovered.", b"Service restored; investigation complete.")
        updated, _ = p.collect(archive, health, later, changed)
        self.assertEqual(len(updated), len(archive))
        record = next(r for r in updated if r["id"] == "github:abc123")
        self.assertEqual(record["first_seen"], "2026-11-17T08:00:00Z")
        self.assertEqual(record["last_seen"], "2026-11-18T08:00:00Z")
        self.assertEqual(len(record["revisions"]), 1)
        self.assertIn("recovered", record["revisions"][0]["response"])

    def test_partial_failure_keeps_records_and_last_success(self):
        archive, health = p.collect([], {}, NOW, self.fetch)
        def partial(source):
            if source["id"] == "github":
                raise OSError("network unavailable")
            return self.fetch(source)
        updated, report = p.collect(archive, health, NOW, partial)
        self.assertIn("github:abc123", {r["id"] for r in updated})
        failed = next(s for s in report["sources"] if s["id"] == "github")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["last_success"], "2026-11-17T08:00:00Z")
        self.assertIn("network unavailable", failed["error"])
        self.assertEqual(report["status"], "partial")

    def test_all_fail_does_not_mutate_archive(self):
        archive, health = p.collect([], {}, NOW, self.fetch)
        original = copy.deepcopy((archive, health))
        def failed(source):
            raise OSError("offline")
        with self.assertRaises(p.CollectionFailed):
            p.collect(archive, health, NOW, failed)
        self.assertEqual((archive, health), original)

    def test_old_tracked_status_can_receive_update(self):
        records, _ = p.parse_source(p.SOURCES[0], json.dumps(STATUS).encode(), NOW)
        records[0]["first_seen"] = "2026-11-17T08:00:00Z"
        records[0]["last_seen"] = records[0]["first_seen"]
        records[0]["revisions"] = []
        later = datetime(2027, 1, 1, tzinfo=timezone.utc)
        archive, _ = p.collect(records, {}, later, self.fetch)
        tracked = next(r for r in archive if r["id"] == "github:abc123")
        self.assertEqual(tracked["last_seen"], "2027-01-01T00:00:00Z")


class BuildTests(unittest.TestCase):
    def test_static_library_is_latest_with_date_fallback_and_sort_metadata(self):
        records, health = p.collect([], {}, NOW, CollectionTests().fetch)
        base = records[0]
        entries = [
            dict(base, id="historical", historical=True, incident_date="2018-10-21T22:52:00Z"),
            dict(base, id="newer", incident_date=None, published_at="2026-11-17T10:00:00Z"),
            dict(base, id="older", incident_date="2026-11-17T09:00:00Z",
                 published_at="2026-11-18T10:00:00Z"),
        ]
        entries[0]["lessons"] = [" First lesson ", "first   LESSON", "Second lesson"]
        with tempfile.TemporaryDirectory() as folder:
            p.build(entries[1:], entries[:1], health, Path(folder))
            page = (Path(folder) / "index.html").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<article[^>]+id="([^"]+)"', page),
                         ["newer", "older", "historical"])
        historical = re.search(r'<article[^>]+id="historical"[^>]+>', page).group()
        self.assertIn('data-learning-tier="2"', historical)
        self.assertIn('data-lesson-count="2"', historical)
        feed = re.search(r'<article[^>]+id="newer"[^>]+>', page).group()
        self.assertIn('data-lesson-count="0"', feed)
        self.assertIn('data-order-date="2026-11-17T10:00:00Z"', feed)
        self.assertIn('<option value="latest">Latest</option>', page)
        self.assertIn('<option value="learnings">Most learnings</option>', page)

    def test_template_tokens_from_feed_remain_literal_text(self):
        records, health = p.collect([], {}, NOW, CollectionTests().fetch)
        records[0]["title"] = "{{CASE_LINKS}}"
        with tempfile.TemporaryDirectory() as folder:
            p.build(records, [], health, Path(folder))
            page = (Path(folder) / "index.html").read_text(encoding="utf-8")
            self.assertIn("<h3>{{CASE_LINKS}}</h3>", page)

    def test_browser_script_is_served_as_javascript_by_local_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            p.build([], [], {}, Path(folder))
            page = (Path(folder) / "index.html").read_text(encoding="utf-8")
            script = re.search(r'<script[^>]+src="([^"]+)"', page).group(1)
            self.assertTrue((Path(folder) / script).exists())
            self.assertIn(mimetypes.guess_type(script)[0],
                          {"application/javascript", "text/javascript"})

    def test_build_escapes_feed_text_and_unsafe_links(self):
        records, health = p.collect([], {}, NOW, CollectionTests().fetch)
        records[0].update(title='<img src=x onerror="alert(1)">', url="javascript:alert(1)")
        records[0]["summary"] = "</script><script>alert(1)</script>"
        with tempfile.TemporaryDirectory() as folder:
            p.build(records, [], health, Path(folder))
            html = (Path(folder) / "index.html").read_text(encoding="utf-8")
            self.assertNotIn('<img src=x', html)
            self.assertNotIn('href="javascript:', html)
            self.assertNotIn("</script><script>alert", html)
            self.assertIn("&lt;img", html)
            self.assertIn("No incidents match", html)

    def test_build_rejects_duplicate_ids_and_shows_empty_collection(self):
        records, health = p.collect([], {}, NOW, CollectionTests().fetch)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                p.build(records + records, [], health, Path(folder))
            p.build([], [], {}, Path(folder))
            html = (Path(folder) / "index.html").read_text(encoding="utf-8")
            self.assertIn("No successful collection yet", html)
            self.assertIn("No incidents collected", html)


if __name__ == "__main__":
    unittest.main()
