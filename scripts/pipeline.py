"""Collect a deliberately selected incident feed and render a dependency-free site."""

import argparse
import copy
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(ROOT))
SOURCES = [
    {"id": "github", "name": "GitHub Status", "kind": "statuspage",
     "url": "https://www.githubstatus.com/api/v2/incidents.json"},
    {"id": "cloudflare", "name": "Cloudflare Status", "kind": "statuspage",
     "url": "https://www.cloudflarestatus.com/api/v2/incidents.json"},
    {"id": "bleepingcomputer", "name": "BleepingComputer", "kind": "rss",
     "url": "https://www.bleepingcomputer.com/feed/"},
    {"id": "hackernews", "name": "The Hacker News", "kind": "rss",
     "url": "https://feeds.feedburner.com/TheHackersNews"},
]
CAUSES = {
    "unknown": "Cause not yet known",
    "deployment": "Deployment / change",
    "capacity": "Capacity / resources",
    "dependency": "Dependency / network",
    "configuration": "Configuration",
    "security": "Security attack",
}
LOOKBACK_DAYS = 30
MAX_PER_SOURCE = 20
MAX_BYTES = 4_000_000
FETCH_DEADLINE = 40
DOWNLOAD_COMMAND = [sys.executable, str(Path(__file__).resolve()), "download"]
SECURITY_EVENT = re.compile(
    r"\b(?:breach(?:ed)?|stol[ea]n?|stole|hacked|hijacked|infected|"
    r"ransomware attack|supply.chain attack|unauthorized access|"
    r"exfiltrat\w*|attackers exploit|actively exploit\w*)\b", re.I)
OUTAGE_EVENT = re.compile(r"\b(?:outage|disruption|service failure)\b", re.I)
MAJOR_SIGNAL = re.compile(
    r"\b(?:millions?|thousands?|global|worldwide|widespread|supply.chain|"
    r"hospitals?|government|payment|critical infrastructure|"
    r"customer data|customer information|cloud (?:environment|breach)|"
    r"multiple (?:companies|organizations))\b", re.I)
EXCLUDED_NEWS = re.compile(
    r"\b(?:proof.of.concept|simulat\w*|safety test\w*|"
    r"no (?:evidence of |observed )?(?:exploitation|breach)|"
    r"denies (?:a |data )?breach|"
    r"arrested|extradited|sentenced|faces \d+ years)\b", re.I)


class CollectionFailed(RuntimeError):
    pass


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def text(value, limit=1200):
    parser = TextOnly()
    parser.feed(str(value or ""))
    cleaned = " ".join("".join(parser.parts).split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit].rsplit(" ", 1)[0] + "..."


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def date(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Missing publication/update date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"Date has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value or "")
        if (parts.scheme not in {"https", "http"} or not parts.hostname
                or parts.username or parts.password
                or any(ord(c) < 33 for c in value)):
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query)
                           if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}])
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, ""))
    except ValueError:
        return None


def learning_context(kind, evidence):
    """These prompts are general practice, never inferred incident findings."""
    if re.search(r"database|replica|mysql|data integrity", evidence, re.I):
        return ([
            "Draw the write path and the failover path separately. A healthy replica is not proof that an application can use the new topology.",
            "Practice restoring data and reconciling divergent writes; measure recovery under realistic bandwidth and load.",
        ], ["Which data-integrity invariant would you refuse to break to restore availability?",
            "How would you verify that a recovered replica contains every acknowledged write?"])
    if re.search(r"supply.chain|scripts?|third.party", evidence, re.I) and kind == "security":
        return ([
            "Map third-party code and credentials that cross your trust boundary; constrain what each dependency can access.",
            "Practice revoking compromised integrations and preserving evidence before restoring trust. These are review ideas, not a reconstruction of this attack.",
        ], ["Which external dependency can execute code in your users' browsers or production systems?",
            "Can you disable that dependency without losing your ability to investigate?"])
    if kind == "security":
        return ([
            "Map access to sensitive data, including backups and integration tokens. Minimize privileges and make unusual access observable.",
            "Prepare a containment and credential-rotation checklist. A report of theft alone does not establish how access was obtained.",
        ], ["What evidence would confirm the scope of access rather than just the initial alert?",
            "Which credentials and downstream systems would your containment plan need to examine?"])
    return ([
        "Map user-visible dependencies and test partial failure, not just complete downtime. Separate detection, mitigation, and full recovery.",
        "Practice a rollback or isolation decision using an external health signal. Do not assume the provider's recovery means your own backlog has cleared.",
    ], ["What user-visible signal would alert you before support tickets arrive?",
        "How would you keep serving a useful degraded experience while a dependency recovers?"])


def live_record(source, identity, title, url, published, updated, incident_date, kind,
                summary, impact, response, evidence, selection, status):
    lessons, questions = learning_context(kind, title + " " + " ".join(e["text"] for e in evidence))
    return {
        "id": identity, "title": title, "url": url, "source_id": source["id"],
        "source_name": source["name"], "type": kind, "cause": "unknown",
        "historical": False, "incident_date": incident_date,
        "published_at": published, "source_updated_at": updated, "status": status,
        "summary": summary, "impact": impact, "response": response,
        "what_happened": (
            f"{source['name']} published this selected {'service incident' if source['kind'] == 'statuspage' else 'report'}. "
            "The attributed extracts below are the available evidence, not an independently verified investigation."
        ),
        "known_cause": "Not yet known in this library. A cause is not inferred from a headline or keyword. Check the linked source for confirmed findings.",
        "lessons": lessons, "questions": questions, "evidence": evidence,
        "selection_reason": selection,
        "explanation_note": "Automated detailed explanation unavailable: this entry uses feed evidence only. General lessons are study prompts, not findings about this incident.",
    }


def parse_source(source, body, now, tracked=frozenset()):
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    records = []
    if source["kind"] == "statuspage":
        document = json.loads(body)
        if not isinstance(document, dict) or not isinstance(document.get("incidents"), list):
            raise ValueError("Expected Statuspage incidents array")
        entries = document["incidents"]
        for item in entries[:100]:
            if not isinstance(item, dict):
                raise ValueError("Incident must be an object")
            identity = f"{source['id']}:{item['id']}"
            if identity not in tracked and item.get("impact") not in {"major", "critical"}:
                continue
            published = date(item["created_at"])
            started = date(item.get("started_at") or item["created_at"])
            updated = date(item["updated_at"])
            if identity not in tracked and (started < cutoff or started > now + timedelta(hours=1)):
                continue
            url = safe_url(item.get("shortlink"))
            if not url:
                raise ValueError("Incident source link is not a safe HTTP(S) URL")
            raw_updates = item.get("incident_updates") or []
            if not isinstance(raw_updates, list) or any(not isinstance(u, dict) for u in raw_updates):
                raise ValueError("Incident updates must be an array of objects")
            updates = sorted(raw_updates,
                             key=lambda u: date(u.get("display_at") or u["created_at"]))
            # First impact statement and latest updates, not an entire provider transcript.
            chosen = ([updates[0]] + updates[-3:]) if len(updates) > 3 else updates
            evidence = []
            seen = set()
            for update in chosen:
                excerpt = text(update.get("body"), 360)
                if excerpt and excerpt not in seen:
                    evidence.append({"at": stamp(date(update.get("display_at") or update["created_at"])),
                                     "text": excerpt})
                    seen.add(excerpt)
            title = text(item["name"], 220)
            first = evidence[0]["text"] if evidence else "The provider did not include a description."
            latest = evidence[-1]["text"] if evidence else "No recovery information is available in this feed."
            records.append(live_record(
                source, identity, title, url, stamp(published), stamp(updated), stamp(started),
                "outage", f"{source['name']} reported a {item['impact']}-impact service incident: {title}.",
                first, latest, evidence, f"Provider classified impact as {item['impact']}.", text(item["status"], 50)))
    else:
        root = ET.fromstring(body)
        atom = root.tag == "{http://www.w3.org/2005/Atom}feed"
        channel = root.find("channel")
        if not atom and (root.tag != "rss" or channel is None):
            raise ValueError("Expected RSS channel or Atom feed")
        ns = "{http://www.w3.org/2005/Atom}" if atom else ""
        entries = root.findall(ns + "entry") if atom else channel.findall("item")
        for item in entries[:100]:
            def field(name):
                element = item.find(ns + name)
                return "".join(element.itertext()) if element is not None else ""
            title = text(field("title"), 220)
            description = text(field("summary" if atom else "description"), 240)
            link = item.find(ns + "link")
            if atom:
                links = [e for e in item.findall(ns + "link") if e.get("rel", "alternate") == "alternate"]
                raw_link = links[0].get("href", "") if links else ""
            else:
                raw_link = link.text if link is not None else ""
            url = safe_url(raw_link)
            if not url:
                raise ValueError("News source link is not a safe HTTP(S) URL")
            identity = "report:" + hashlib.sha256(url.encode()).hexdigest()[:20]
            published = date(field("published") or field("updated")) if atom else date(field("pubDate"))
            updated = date(field("updated")) if atom and field("updated") else None
            if identity not in tracked:
                if published < cutoff or published > now + timedelta(hours=1):
                    continue
                combined = title + " " + description
                if EXCLUDED_NEWS.search(combined) or not MAJOR_SIGNAL.search(combined):
                    continue
                # Require the headline itself to report an event; article teasers alone
                # often mention attacks while advertising unrelated vulnerability news.
                if not SECURITY_EVENT.search(title) and not OUTAGE_EVENT.search(title):
                    continue
            kind = "outage" if OUTAGE_EVENT.search(title) else "security"
            excerpt = description or title
            records.append(live_record(
                source, identity, title, url, stamp(published), stamp(updated) if updated else None, None, kind,
                f"Selected reporting from {source['name']}: {title}.",
                "The full scope is not independently established here. See the attributed feed extract and original report.",
                "Recovery or containment is not established by this feed extract. Consult the original report for updates.",
                [{"at": stamp(published), "text": excerpt}],
                "Event headline plus a significant-impact keyword; automated candidate selection, not a verified severity rating.",
                "reported"))
    records.sort(key=lambda r: r["source_updated_at"] or r["published_at"], reverse=True)
    # Tracked records are updated even when the daily intake limit has been reached.
    existing = [r for r in records if r["id"] in tracked]
    new = [r for r in records if r["id"] not in tracked][:MAX_PER_SOURCE]
    return existing + new, min(len(entries), 100)


def download(source):
    request = Request(source["url"], headers={
        "User-Agent": "IncidentLearning/1.0 (+https://github.com/vikhyatcode/incident-learning)",
        "Accept": "application/json, application/rss+xml, application/atom+xml, application/xml, text/xml",
    })
    with urlopen(request, timeout=30) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("Source response exceeds 4 MB limit")
    return body


def fetch(source):
    # A subprocess deadline also bounds slow streaming and repeated redirects.
    # Socket timeouts alone reset whenever another chunk arrives.
    try:
        result = subprocess.run(DOWNLOAD_COMMAND + [source["id"]], capture_output=True,
                                timeout=FETCH_DEADLINE, check=True)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Source exceeded wall-clock download deadline") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace")[-800:].strip()
        raise OSError(f"Download failed: {detail}") from error
    return result.stdout


def collect(archive, previous, now, fetcher=fetch):
    saved = {record["id"]: copy.deepcopy(record) for record in archive}
    old_health = {s["id"]: s for s in previous.get("sources", [])}
    health = []
    successful = 0
    for source in SOURCES:
        last_success = old_health.get(source["id"], {}).get("last_success")
        entry = {**source, "checked_at": stamp(now), "last_success": last_success}
        try:
            records, scanned = parse_source(source, fetcher(source), now, frozenset(saved))
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
            entry.update(status="error", error=f"{type(error).__name__}: {error}", selected=0)
            print(f"Source error [{source['id']}]: {entry['error']}", file=sys.stderr)
        else:
            successful += 1
            entry.update(status="ok", last_success=stamp(now), selected=len(records), scanned=scanned)
            for record in records:
                old = saved.get(record["id"])
                record["first_seen"] = old["first_seen"] if old else stamp(now)
                record["last_seen"] = stamp(now)
                revisions = copy.deepcopy(old.get("revisions", [])) if old else []
                if old and any(old.get(k) != record.get(k) for k in (
                        "summary", "impact", "response", "evidence", "title", "status")):
                    revisions.append({k: old.get(k) for k in (
                        "source_updated_at", "title", "status", "impact", "response", "evidence")})
                record["revisions"] = revisions[-10:]
                saved[record["id"]] = record
        health.append(entry)
    if not successful:
        raise CollectionFailed("All sources failed; archive and published site must remain unchanged.")
    report = {"collected_at": stamp(now), "status": "ok" if successful == len(SOURCES) else "partial",
              "sources": health, "archive_count": len(saved)}
    return sorted(saved.values(), key=lambda r: (r["published_at"], r["id"]), reverse=True), report


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def esc(value):
    return html.escape(str(value or ""), quote=True)


def source_link(url, label):
    clean = safe_url(url)
    return f'<a href="{esc(clean)}" rel="noopener noreferrer">{esc(label)} <span aria-hidden="true">&nearr;</span></a>' if clean else "<span>Source link unavailable (unsafe URL rejected)</span>"


def ordering(record):
    value = record.get("incident_date") or record["published_at"]
    timestamp = stamp(date(value + "T00:00:00Z" if len(value) == 10 else value))
    tier = 2 if record["historical"] or record.get("explanation_status") == "reviewed" else (
        1 if record.get("explanation_status") == "timeline" else 0)
    lessons = len({" ".join(lesson.split()).casefold() for lesson in record["lessons"]
                   if lesson.strip()}) if tier else 0
    return timestamp, tier, lessons


def render_card(record):
    r = record
    cause = CAUSES[r["cause"]]
    historical = r["historical"]
    label = "Historical case study" if historical else "Collected report"
    display_date = (r.get("incident_date") or r["published_at"])[:10]
    order_date, learning_tier, lesson_count = ordering(r)
    date_label = "Incident" if r.get("incident_date") else "Published; incident date unconfirmed"
    evidence = "".join(
        f'<blockquote><p>{esc(e["text"])}</p><cite>{esc(r["source_name"])} &middot; {esc(e["at"])}</cite></blockquote>'
        for e in r.get("evidence", []))
    citations = "".join(
        f'<li><strong>{esc(c["section"].replace("_", " ").title())}</strong>: '
        f'&ldquo;{esc(c["text"])}&rdquo; '
        f'{source_link(c["url"], r["source_name"])}</li>'
        for c in r.get("citations", []))
    lessons = "".join(f"<li>{esc(v)}</li>" for v in r["lessons"])
    questions = "".join(f"<li>{esc(v)}</li>" for v in r["questions"])
    references = "".join("<li>" + source_link(s["url"], s["name"]) + "</li>" for s in r.get("references", []))
    revisions = r.get("revisions", [])
    history = ""
    if revisions:
        history = '<details class="revision"><summary>Earlier source snapshots (' + str(len(revisions)) + ')</summary>'
        for version in reversed(revisions):
            history += f'<p><strong>{esc(version["source_updated_at"])}</strong> &middot; {esc(version["status"])}</p><p>{esc(version["response"])}</p>'
        history += "</details>"
    search = " ".join(str(r.get(k, "")) for k in ("title", "summary", "source_name", "impact", "known_cause", "what_happened"))
    return f"""
    <article class="incident-card {'case-study' if historical else ''}" id="{esc(r['id'])}"
      data-type="{esc(r['type'])}" data-cause="{esc(r['cause'])}"
      data-kind="{'historical' if historical else 'live'}" data-date="{esc(display_date)}" data-search="{esc(search.lower())}"
      data-id="{esc(r['id'])}" data-order-date="{esc(order_date)}" data-learning-tier="{learning_tier}" data-lesson-count="{lesson_count}">
      <div class="card-top"><span class="eyebrow">{esc(label)}</span><span class="status">{esc(r['status'].replace('_', ' '))}</span></div>
      <h3>{esc(r['title'])}</h3>
      <p class="card-summary">{esc(r['summary'])}</p>
      <div class="tags"><span class="tag {esc(r['type'])}">{'Security incident' if r['type'] == 'security' else 'Outage / failure'}</span><span class="tag">{esc(cause)}</span></div>
      <p class="date-line">{esc(date_label)}: <time datetime="{esc(display_date)}">{esc(display_date)}</time></p>
      <details class="lesson"><summary>Explore the incident <span aria-hidden="true">+</span></summary>
        <div class="lesson-content">
          <p class="evidence-note">{esc(r.get('explanation_note', 'Originally written case study, based on the linked postmortem. Facts and general lessons are separated below.'))}</p>
          {('<p class="warning">Explanation update unavailable: ' + esc(r["enrichment_error"]) + '</p>') if r.get("enrichment_error") else ''}
          <div class="fact-grid">
            <section><h4>01 &nbsp; What happened</h4><p>{esc(r['what_happened'])}</p></section>
            <section><h4>02 &nbsp; Impact</h4><p>{esc(r['impact'])}</p></section>
            <section><h4>03 &nbsp; Known cause</h4><p>{esc(r['known_cause'])}</p></section>
            <section><h4>04 &nbsp; Response &amp; recovery</h4><p>{esc(r['response'])}</p></section>
          </div>
          {('<section><h4>Source evidence &mdash; short attributed extracts</h4>' + evidence + '</section>') if evidence else ''}
          {('<section><h4>Source citations by section</h4><ul>' + citations + '</ul></section>') if citations else ''}
          <section class="study-panel"><h4>Engineering lessons <span>General practice, not additional incident facts</span></h4><ul>{lessons}</ul>
            <h4>Pause &amp; reflect</h4><ol>{questions}</ol></section>
          <div class="provenance"><p>{source_link(r['url'], r['source_name'])}</p>
            <p>Source published: {esc(r['published_at'])}<br>Source updated: {esc(r['source_updated_at'] or 'Not provided by feed')}</p>
            {('<p>First collected: ' + esc(r['first_seen']) + '<br>Last seen in feed: ' + esc(r['last_seen']) + '</p>') if r.get('first_seen') else ''}
            {('<p>Selection: ' + esc(r['selection_reason']) + '</p>') if r.get('selection_reason') else ''}
            {('<ul>' + references + '</ul>') if references else ''}
          </div>{history}
        </div>
      </details>
    </article>"""


def build(archive, cases, health, output):
    records = cases + archive
    seen = set()
    for record in records:
        if record["id"] in seen:
            raise ValueError(f"Duplicate record ID: {record['id']}")
        seen.add(record["id"])
        if record["cause"] not in CAUSES or record["type"] not in {"security", "outage"}:
            raise ValueError("Unsupported cause or incident type")
    records.sort(key=lambda r: r["id"])
    records.sort(key=lambda r: ordering(r)[0], reverse=True)
    output.mkdir(parents=True, exist_ok=True)
    template = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    source_rows = []
    for s in health.get("sources", []):
        result = (f'{s["selected"]} selected / {s["scanned"]} inspected'
                  if s["status"] == "ok" else f'Failed: {s["error"]}')
        source_rows.append(f'<li><div>{source_link(s["url"], s["name"])}<span class="health-{esc(s["status"])}">{esc(s["status"].upper())}</span></div><p>{esc(result)}<br>Last success: {esc(s.get("last_success") or "Never")}</p></li>')
    values = {
        "CARDS": "".join(render_card(r) for r in records) or '<p class="empty">No incidents collected. Check source health and the workflow.</p>',
        "SOURCES": "".join(source_rows) or "<li>No successful collection yet</li>",
        "COLLECTED_AT": esc(health.get("collected_at", "")),
        "COLLECTION_LABEL": esc(health.get("collected_at", "No successful collection yet")),
        "HEALTH_LABEL": "Partial source failure" if health.get("status") == "partial" else ("Sources healthy" if health.get("status") == "ok" else "Awaiting collection"),
        "LIVE_COUNT": str(len(archive)),
        "CASE_COUNT": str(len(cases)),
        "EXPLANATION_HEALTH": esc(
            f'{health["explanations"]["explained"]} of {health["explanations"]["total"]} current reports explained '
            f'with source-reviewed analysis; {health["explanations"].get("timelines", 0)} automated provider timelines. '
            f'Explanation status: {health["explanations"]["status"]}. '
            f'Checked: {health["explanations"]["checked_at"]}. '
            'Other reports retain clearly labeled feed-only evidence.'
        ) if health.get("explanations") else "Current explanation checks have not run yet.",
        "CAUSE_OPTIONS": "".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in CAUSES.items()),
        "CASE_LINKS": "".join(f'<a href="#{esc(r["id"])}" class="case-link" data-case-link><span>{esc(r["title"])}</span><small>{esc(CAUSES[r["cause"]])} &rarr;</small></a>' for r in cases),
    }
    template = re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values[match.group(1)], template)
    (output / "index.html").write_text(template, encoding="utf-8")
    for source, target in (("style.css", "style.css"), ("app.mjs", "app.js")):
        shutil.copyfile(ROOT / "web" / source, output / target)
    write_json(output / "incidents.json", {"incidents": records, "collection": health})
    (output / ".nojekyll").touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "build", "download"])
    parser.add_argument("source", nargs="?", choices=[s["id"] for s in SOURCES])
    args = parser.parse_args()
    if args.command == "download":
        if not args.source:
            parser.error("download requires a configured source ID")
        try:
            sys.stdout.buffer.write(download(next(s for s in SOURCES if s["id"] == args.source)))
        except (OSError, ValueError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
            return 1
        return 0
    archive_path, health_path = ROOT / "data" / "incidents.json", ROOT / "data" / "collection.json"
    archive, health = read_json(archive_path, []), read_json(health_path, {})
    if args.command == "collect":
        try:
            archive, health = collect(archive, health, datetime.now(timezone.utc))
        except CollectionFailed as error:
            print(error, file=sys.stderr)
            return 1
        write_json(archive_path, archive)
        write_json(health_path, health)
        print(f"Archived {len(archive)} selected reports. Source health: {health['status']}.")
    else:
        from scripts import enrichment
        stored = read_json(ROOT / "data" / "explanations.json", {})
        archive = enrichment.for_build(
            archive, read_json(ROOT / "data" / "current-explanations.json", {}), stored)
        if stored.get("health"):
            explanation_health = dict(stored["health"])
            explanation_health["explained"] = sum(
                r.get("explanation_status") == "reviewed" for r in archive)
            explanation_health["timelines"] = sum(
                r.get("explanation_status") == "timeline" for r in archive)
            explanation_health["total"] = len(archive)
            explanation_health["pending"] = sum(bool(r.get("enrichment_error")) for r in archive)
            explanation_health["status"] = "partial" if explanation_health["pending"] else "ok"
            health["explanations"] = explanation_health
        build(archive, read_json(ROOT / "data" / "case-studies.json", []), health, ROOT / "site")
        print(f"Built site with {len(archive)} collected reports.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
