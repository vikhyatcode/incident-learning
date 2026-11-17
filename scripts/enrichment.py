"""Verify current explainers against allowlisted evidence and cache short citations."""

import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts import pipeline as p


VERSION = 1
ARTICLE_HOSTS = {
    "www.bleepingcomputer.com": re.compile(r"^/news/[a-z0-9-]+/[a-z0-9-]+/$"),
    "thehackernews.com": re.compile(r"^/\d{4}/\d{2}/[a-z0-9-]+\.html$"),
}
FIELDS = ("what_happened", "impact", "known_cause", "response")
UNKNOWN = "Not established by the available source evidence."


def article_url(url):
    clean = p.safe_url(url)
    if not clean:
        raise ValueError("Unsafe article URL")
    parts = urlsplit(clean)
    pattern = ARTICLE_HOSTS.get(parts.hostname)
    if (parts.scheme != "https" or parts.port not in (None, 443) or not pattern
            or parts.query or unquote(parts.path) != parts.path
            or not pattern.fullmatch(parts.path)):
        raise ValueError("Article URL is not an allowlisted public article path")
    return clean


class ArticleRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        article_url(newurl)
        if req is not None and urlsplit(req.full_url).hostname != urlsplit(newurl).hostname:
            raise ValueError("Cross-host article redirect rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ArticleText(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body_depth = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.parts.append(" ")
        if tag in self.VOID:
            return
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        hidden = (bool(self.stack and self.stack[-1][1])
                  or tag in {"script", "style", "aside", "figure", "table"}
                  or "article-callout" in classes)
        self.stack.append((tag, hidden))
        if self.body_depth is None and (
                "articleBody" in classes or "articlebody" in classes):
            self.body_depth = len(self.stack)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break
        if self.body_depth is not None and len(self.stack) < self.body_depth:
            self.body_depth = None
        self.parts.append(" ")

    def handle_data(self, data):
        if self.body_depth is not None and not self.stack[-1][1]:
            self.parts.append(data)


def extract_article(body):
    parser = ArticleText()
    parser.feed(body)
    result = " ".join("".join(parser.parts).split())
    if len(result) < 80:
        raise ValueError("No substantial recognized article body")
    return result[:24000]


def download_article(url):
    url = article_url(url)
    request = Request(url, headers={"User-Agent": "IncidentLearning/1.0",
                                    "Accept": "text/html"})
    with build_opener(ArticleRedirect()).open(request, timeout=20) as response:
        article_url(response.url)
        if "text/html" not in response.headers.get("Content-Type", ""):
            raise ValueError("Article response is not HTML")
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("Article exceeds 2 MB")
    return extract_article(body.decode("utf-8", errors="replace"))


def fetch_article(url):
    article_url(url)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.enrichment", "download", url],
            timeout=30, check=True, capture_output=True)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("Article exceeded 30-second wall-clock deadline") from error
    except subprocess.CalledProcessError as error:
        raise OSError(error.stderr.decode(errors="replace")[-600:].strip()) from error
    return result.stdout.decode("utf-8").strip()


def plain(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or "<" in value or ">" in value or any(ord(c) < 32 for c in value)):
        raise ValueError("Invalid explanation text")
    return value


def validate(value, evidence):
    if not isinstance(value, dict) or set(value) != {
            "summary", *FIELDS, "cause", "lessons", "questions"}:
        raise ValueError("Unexpected explanation fields")
    plain(value["summary"], 450)
    if value["cause"] not in p.CAUSES:
        raise ValueError("Unsupported cause")
    quotes = set()
    for field in FIELDS:
        fact = value[field]
        if not isinstance(fact, dict) or set(fact) != {"text", "quotes"}:
            raise ValueError("Fact requires text and source quotes")
        plain(fact["text"], 1400)
        if not isinstance(fact["quotes"], list) or len(fact["quotes"]) > 2:
            raise ValueError("Invalid citation list")
        if not fact["quotes"] and (fact["text"] != UNKNOWN or
                                  field == "known_cause" and value["cause"] != "unknown"):
            raise ValueError("Unsupported fact or cause requires source evidence")
        for quote in fact["quotes"]:
            plain(quote, 220)
            if len(quote) < 20 or quote not in evidence:
                raise ValueError("Citation is not an exact source excerpt")
            quotes.add(quote)
    if sum(len(quote.split()) for quote in quotes) > 80:
        raise ValueError("Citations exceed the 80-word per-report quotation budget")
    for key in ("lessons", "questions"):
        if not isinstance(value[key], list) or not 2 <= len(value[key]) <= 4:
            raise ValueError("Expected bounded lesson/question list")
        for entry in value[key]:
            plain(entry, 1000)
    return copy.deepcopy(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fingerprint(record):
    return digest({"version": VERSION, "record": {
        key: record.get(key) for key in
        ("title", "url", "source_id", "source_updated_at", "evidence",
         "explanation_revision")}})


def apply_explanation(record, cached):
    result = copy.deepcopy(record)
    explanation = cached["explanation"]
    for key in ("summary", "cause", "lessons", "questions"):
        result[key] = explanation[key]
    for key in FIELDS:
        result[key] = explanation[key]["text"]
    result["evidence"] = []
    result["citations"] = [
        {"section": field, "text": quote, "url": record["url"]}
        for field in FIELDS for quote in explanation[field]["quotes"]]
    result["explanation_note"] = (
        "Source-reviewed current explanation, originally written for this notebook. "
        f"Evidence checked {cached['checked_at']}. Claims are attributed to the linked source; "
        "cause grouping reflects that account, not independent verification. "
        "Automated AI explanation: unavailable; this is a reviewed explanation with automatic citation checks.")
    result["explanation_status"] = "reviewed"
    return result


def enrich(records, cache, now, loader, explainer, budget=8):
    result = []
    attempted = 0
    errors = 0
    for record in records:
        old = cache.get(record["id"])
        key = fingerprint(record)
        # Status feeds have explicit update timestamps; unchanged records need no new download.
        if old and old["fingerprint"] == key and record["source_id"] in {"github", "cloudflare"}:
            result.append(apply_explanation(record, old))
            continue
        candidate = copy.deepcopy(record)
        if attempted >= budget:
            candidate["enrichment_error"] = "Daily evidence-check budget reached; feed-only evidence is shown."
            errors += 1
        else:
            attempted += 1
            try:
                evidence = loader(record)
                source_hash = digest(evidence)
                if old and old["fingerprint"] == key and old["source_hash"] == source_hash:
                    old["checked_at"] = now
                    old["explanation_revision"] = record.get("explanation_revision")
                    candidate = apply_explanation(record, old)
                else:
                    explanation = validate(explainer(record, evidence), evidence)
                    cached = {"fingerprint": key, "source_hash": source_hash,
                              "explanation_revision": record.get("explanation_revision"),
                              "checked_at": now, "explanation": explanation}
                    cache[record["id"]] = cached
                    candidate = apply_explanation(record, cached)
            except (OSError, ValueError, KeyError, TypeError) as error:
                candidate["enrichment_error"] = f"{type(error).__name__}: {error}"
                errors += 1
        result.append(candidate)
    return result, {"checked_at": now, "status": "partial" if errors else "ok",
                    "attempted": attempted, "explained": sum(
                        r.get("explanation_status") == "reviewed" for r in result),
                    "pending": errors, "total": len(records)}


def provider_timeline(record):
    result = copy.deepcopy(record)
    evidence = record.get("evidence", [])
    if not evidence:
        return result
    first, last = evidence[0], evidence[-1]
    name = record["source_name"]
    result["summary"] = (
        f"{name} recorded {record['title']} and currently marks it {record['status']}. "
        f"The retained notices span {first['at']} to {last['at']}; "
        "this is a reporting timeline, not a measured outage duration.")
    result["what_happened"] = (
        f"The provider tracked this as {record['title']}. The first retained notice "
        f"is dated {first['at']}. Read the attributed impact statement below for the "
        "affected operation; a service incident does not mean every request or every user failed.")
    result["impact"] = f"{name}'s initial retained impact statement: {first['text']}"
    result["response"] = (
        f"The latest retained notice is dated {last['at']}, with provider incident "
        f"status {record['status']}: {last['text']} "
        "A resolved status documents the provider's recovery assessment, not an explanation of the root cause.")
    result["known_cause"] = (
        "Not established by the retained provider evidence. Resolution and mitigation "
        "notices are not themselves a root-cause analysis.")
    result["cause"] = "unknown"
    title = record["title"].lower()
    if re.search(r"actions|checks|pages", title):
        lesson = (
            "For this build/deployment-related disruption, distinguish a job accepted "
            "by the control plane from one that has actually started and completed. "
            "After recovery, measure backlog age before declaring your deployment pipeline healthy.")
        question = "Which queued builds or deployments would need reconciliation after this provider recovers?"
    elif re.search(r"pull requests|git operations", title):
        lesson = (
            "For this repository-operation disruption, separate a timed-out client request "
            "from a confirmed failed write. Check the resulting repository state before "
            "retrying a merge; blind retries can obscure whether the first operation completed.")
        question = "How would your automation confirm whether a timed-out merge actually committed?"
    elif "copilot" in title:
        lesson = (
            "For this assistant/model availability disruption, keep essential build and "
            "review paths independent of the assistant. Use bounded retries and a "
            "clearly communicated manual fallback rather than silently waiting forever.")
        question = "Which engineering tasks can continue safely when your assistant dependency is unavailable?"
    else:
        lesson = (
            "For this provider disruption, map the affected operation to your own user "
            "journey and define a degraded path. An upstream incident status alone "
            "does not measure your downstream errors or recovery backlog.")
        question = "Which of your user journeys depends on the operation named in this report?"
    result["lessons"] = [
        lesson,
        "Reconstruct detection, mitigation, and recovery as separate checkpoints using "
        "the provider notices. Missing or sparse updates are evidence gaps, not permission "
        "to infer the exact start time, affected population, or cause."]
    result["questions"] = [question,
                           "What evidence would you need before assigning this incident a causal group?"]
    result["explanation_status"] = "timeline"
    result["explanation_note"] = (
        "Automated provider-timeline explanation, not AI-generated analysis. Incident-specific "
        "operations, timestamps, status, and extracts come from the provider feed. "
        "General engineering lessons are separated below; causal findings remain unknown "
        "unless a source-reviewed explanation is available.")
    return result


def run(archive, document, previous, now, loader=fetch_article):
    notes = document["records"]
    cache = copy.deepcopy(previous.get("entries", {}))
    for entry in cache.values():
        entry.setdefault("explanation_revision", digest({
            "reviewed_at": previous.get("reviewed_at"), "note": entry["explanation"]}))
    selected = []
    for record in archive:
        if record["url"] in notes:
            candidate = copy.deepcopy(record)
            candidate["explanation_revision"] = digest({
                "reviewed_at": document["reviewed_at"], "note": notes[record["url"]]})
            selected.append(candidate)

    def explain(record, evidence):
        old = cache.get(record["id"])
        if (old and old["explanation_revision"] == record["explanation_revision"]
                and old["source_hash"] != digest(evidence)):
            raise ValueError("Source article changed since review; explanation needs re-review.")
        return notes[record["url"]]

    explained, report = enrich(selected, cache, now, lambda r: loader(r["url"]), explain)
    report["total"] = len(archive)
    report["timelines"] = sum(r["source_id"] in {"github", "cloudflare"} for r in archive)
    report["unreviewed"] = len(archive) - len(selected) - report["timelines"]
    errors = {r["id"]: r["enrichment_error"] for r in explained if r.get("enrichment_error")}
    if report["unreviewed"]:
        report["status"] = "partial"
    return {"entries": cache, "health": report, "errors": errors,
            "reviewed_at": document["reviewed_at"]}


def for_build(archive, document, stored):
    records = []
    for record in archive:
        candidate = copy.deepcopy(record)
        notes = document.get("records", {}).get(record["url"])
        if notes:
            candidate["explanation_revision"] = digest({
                "reviewed_at": document["reviewed_at"], "note": notes})
        cached = stored.get("entries", {}).get(record["id"])
        error = stored.get("errors", {}).get(record["id"])
        if cached and cached["fingerprint"] == fingerprint(candidate) and not error:
            candidate = apply_explanation(candidate, cached)
        elif record["source_id"] in {"github", "cloudflare"}:
            candidate = provider_timeline(candidate)
        else:
            candidate["enrichment_error"] = error or (
                "No verified current explanation yet. Feed evidence is available; "
                "this report needs source review.")
        if error:
            candidate["enrichment_error"] = error
        records.append(candidate)
    return records


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "download":
        try:
            sys.stdout.buffer.write(download_article(sys.argv[2]).encode("utf-8"))
        except (OSError, ValueError) as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
            return 1
        return 0
    if len(sys.argv) != 1:
        raise SystemExit("Expected no arguments, or download <allowlisted-article-url>")
    archive = p.read_json(p.ROOT / "data" / "incidents.json", [])
    notes = p.read_json(p.ROOT / "data" / "current-explanations.json", {"records": {}})
    path = p.ROOT / "data" / "explanations.json"
    result = run(archive, notes, p.read_json(path, {}), p.stamp(p.datetime.now(p.timezone.utc)))
    p.write_json(path, result)
    print(json.dumps(result["health"]))
    for identity, error in result["errors"].items():
        print(f"Explanation unavailable [{identity}]: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
