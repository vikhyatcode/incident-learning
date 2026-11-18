"""Validate agent-authored judgments; check source freshness without running a model."""

import copy
import re
import sys
from datetime import datetime, timedelta, timezone

from scripts import enrichment as e
from scripts import pipeline as p


WEIGHTS = {"relevance": 5, "detail": 10, "transferability": 7, "confidence": 3}
MAX_CHECKS = 12


def record_hash(record):
    return e.digest({key: record.get(key) for key in (
        "id", "title", "url", "source_updated_at", "evidence", "summary",
        "what_happened", "impact", "known_cause", "response", "lessons", "questions")})


def string(value, limit=1400):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Missing or oversized analysis text")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Analysis text contains control characters")


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Expected SHA-256 evidence fingerprint")


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("Assessment must be an object")
    if value.get("method") != "agent-source-review":
        raise ValueError("Unknown assessment authorship method")
    string(value.get("model"), 100)
    if p.date(value.get("analyzed_at")) > datetime.now(timezone.utc) + timedelta(hours=1):
        raise ValueError("Analysis timestamp is in the future")
    hash_value(value.get("record_hash"))
    sources = value.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 6:
        raise ValueError("Expected one to six reviewed sources")
    for source in sources:
        if not isinstance(source, dict) or not p.safe_url(source.get("url")):
            raise ValueError("Unsafe evidence reference")
        if source.get("kind") not in {"article", "record"}:
            raise ValueError("Unknown evidence type")
        hash_value(source.get("sha256"))
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(WEIGHTS):
        raise ValueError("Expected four learning dimensions")
    for dimension in dimensions.values():
        if not isinstance(dimension, dict) or set(dimension) != {"score", "rationale"}:
            raise ValueError("Dimension needs score and rationale")
        if type(dimension["score"]) is not int or not 0 <= dimension["score"] <= 4:
            raise ValueError("Dimension score must be an integer from zero to four")
        string(dimension["rationale"])
    for key in ("why", "learn"):
        string(value.get(key))
    diagram = value.get("diagram")
    if not isinstance(diagram, dict):
        raise ValueError("Assessment needs a causal explanation")
    for key in ("normal", "root_cause", "contributors", "symptoms"):
        string(diagram.get(key))
    steps = diagram.get("steps")
    if not isinstance(steps, list) or not 4 <= len(steps) <= 9:
        raise ValueError("Expected four to nine explanatory steps")
    stages = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Invalid diagram step")
        stage = step.get("stage")
        if stage not in {"normal", "trigger", "mechanism", "impact", "recovery"}:
            raise ValueError("Invalid diagram stage")
        stages.add(stage)
        if step.get("state") not in {"reported", "unknown", "recommendation"}:
            raise ValueError("Invalid causal evidence state")
        if type(step.get("source")) is not int or not 0 <= step["source"] < len(sources):
            raise ValueError("Diagram references an unread source")
        string(step.get("label"), 100)
        string(step.get("detail"))
    if not {"normal", "trigger", "impact", "recovery"} <= stages:
        raise ValueError("Diagram is missing normal, trigger, impact or recovery")
    if "feedback" in diagram:
        feedback = diagram["feedback"]
        if not isinstance(feedback, dict):
            raise ValueError("Invalid feedback edge")
        for key in ("from", "to"):
            if type(feedback.get(key)) is not int or not 0 <= feedback[key] < len(steps):
                raise ValueError("Feedback edge references an absent node")
        string(feedback.get("label"), 200)
    if "architecture" in value:
        architecture = value["architecture"]
        if not isinstance(architecture, dict):
            raise ValueError("Architecture discussion must be an object")
        for key in ("invariant", "response", "validation", "open_questions"):
            string(architecture.get(key))
        decisions = architecture.get("decisions")
        if not isinstance(decisions, list) or not 2 <= len(decisions) <= 3:
            raise ValueError("Expected two or three proposed architecture alternatives")
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError("Invalid architecture alternative")
            for key in ("title", "proposal", "tradeoff", "residual_risk"):
                string(decision.get(key), 100 if key == "title" else 1400)
            for key in ("from", "to"):
                if type(decision.get(key)) is not int or not 0 <= decision[key] < len(steps):
                    raise ValueError("Architecture alternative references an absent causal step")
            edge = (decision["from"], decision["to"])
            feedback = diagram.get("feedback", {})
            if edge[1] != edge[0] + 1 and edge != (feedback.get("from"), feedback.get("to")):
                raise ValueError("Architecture alternative must address a displayed causal edge")
    return copy.deepcopy(value)


def score(assessment):
    return sum(assessment["dimensions"][key]["score"] * weight
               for key, weight in WEIGHTS.items())


def judgment(assessment):
    d = {key: value["score"] for key, value in assessment["dimensions"].items()}
    if d["relevance"] <= 1 or d["transferability"] <= 1:
        return "Low learning value"
    if d["detail"] < 2 or d["confidence"] < 2 or score(assessment) < 65:
        return "Monitor for details"
    return "Learn now"


def merge_advisories(archive, advisories):
    saved = {record["id"]: copy.deepcopy(record) for record in archive}
    identities, urls = set(), set()
    for record in advisories:
        if (record["id"] in identities or not p.safe_url(record["url"])
                or record["url"] in urls):
            raise ValueError("Duplicate or unsafe curated advisory")
        identities.add(record["id"])
        urls.add(record["url"])
        aliases = record.get("aliases", [])
        if not isinstance(aliases, list) or any(not p.safe_url(url) for url in aliases):
            raise ValueError("Invalid reviewed event aliases")
        same = {record["url"], *aliases}
        saved = {key: item for key, item in saved.items()
                 if item["url"] not in same or key == record["id"]}
        saved[record["id"]] = copy.deepcopy(record)
    return list(saved.values())


def attach(record, assessment, checks, raw=None, collection=None):
    result = copy.deepcopy(record)
    result["analysis_status"] = "unassessed"
    if assessment is None:
        return result
    assessment = validate(assessment)
    raw = raw or record
    reasons = []
    if record_hash(raw) != assessment["record_hash"]:
        reasons.append("Incident evidence or authored content changed since AI review.")
    for source in assessment["sources"]:
        if source["kind"] == "record":
            if source["url"] != raw["url"] or source["sha256"] != record_hash(raw):
                reasons.append("Provider evidence changed since AI review.")
            provider = next((s for s in (collection or {}).get("sources", [])
                             if s["id"] == raw.get("source_id")), {})
            if provider.get("status") != "ok":
                reasons.append("Provider collection check unavailable or failed.")
            else:
                try:
                    times = (provider.get("last_success"), raw.get("last_seen"))
                    ages = [datetime.now(timezone.utc) - p.date(t) for t in times]
                    if any(age > timedelta(hours=48) or age < -timedelta(hours=1) for age in ages):
                        reasons.append("Provider check or retained observation is outside the freshness window.")
                except (TypeError, ValueError):
                    reasons.append("Provider freshness timestamp is invalid.")
        else:
            checked = checks.get("sources", {}).get(source["url"], {})
            if checked.get("status") != "ok":
                reasons.append("Source check unavailable: " + checked.get("error", "not checked"))
            elif checked.get("sha256") != source["sha256"]:
                reasons.append("Source body changed; an agent must re-read it.")
            else:
                try:
                    old = datetime.now(timezone.utc) - p.date(checked.get("checked_at"))
                    if old > timedelta(hours=48) or old < -timedelta(hours=1):
                        reasons.append("Source freshness check is outside the 48-hour window.")
                except (TypeError, ValueError):
                    reasons.append("Source freshness timestamp is invalid.")
    result["analysis_status"] = "stale" if reasons else "assessed"
    result["analysis_note"] = " ".join(dict.fromkeys(reasons))
    result["analysis"] = assessment
    if not reasons:
        result["learning_score"] = score(assessment)
        result["learning_judgment"] = judgment(assessment)
    return result


def render_diagram(assessment):
    diagram = assessment["diagram"]
    sources = assessment["sources"]
    nodes, prose = [], []
    for number, step in enumerate(diagram["steps"], 1):
        state = step["state"]
        nodes.append(f'<li class="causal-node causal-{state}">'
                     f'<span class="causal-number">{number}.</span>'
                     f'<strong>{p.esc(step["label"])}</strong>'
                     f'<small>{p.esc(step["stage"].title())} &middot; {p.esc(state.title())}</small></li>')
        prose.append(f'<li><strong>{p.esc(step["label"])}.</strong> '
                     f'{p.esc(step["detail"])} '
                     f'{p.source_link(sources[step["source"]]["url"], "Source")}</li>')
    feedback = diagram.get("feedback")
    loop = (f'<p class="feedback">Feedback loop: step {feedback["from"] + 1} '
            f'&rarr; step {feedback["to"] + 1}: {p.esc(feedback["label"])}</p>') if feedback else ""
    return (
        '<section class="causal-explanation"><h4>How the failure happened</h4>'
        '<p>Numbered causal map; arrows mean the reported progression, not measured timing. '
        'Unknown links are gaps, not established causes.</p>'
        '<ol class="causal-map" aria-label="Causal map">' + "".join(nodes) + '</ol>' + loop
        + '<dl class="causal-context">'
        + "".join(f'<dt>{label}</dt><dd>{p.esc(diagram[key])}</dd>' for key, label in (
            ("normal", "Normal behavior"), ("root_cause", "Root cause / mechanism"),
            ("contributors", "Contributing factors"), ("symptoms", "Observed symptoms")))
        + '</dl><h4>Read the diagram step by step</h4><ol class="causal-prose">'
        + "".join(prose) + '</ol></section>')


def render_architecture(assessment):
    architecture = assessment.get("architecture")
    if not architecture:
        return ""
    decisions = "".join(
        '<section class="architecture-option"><h5>' + p.esc(option["title"]) + '</h5>'
        f'<p class="evidence-note">Acts on step {option["from"] + 1} &rarr; step {option["to"] + 1} '
        'in the causal map.</p>'
        + f'<p>{p.esc(option["proposal"])}</p>'
        + f'<p><strong>Cost / tradeoff:</strong> {p.esc(option["tradeoff"])}</p>'
        + f'<p><strong>Residual risk:</strong> {p.esc(option["residual_risk"])}</p></section>'
        for option in architecture["decisions"])
    return (
        '<section class="architecture-review"><h4>Architecture choices</h4>'
        '<p class="evidence-note">Proposed alternatives for the reader, not claims about '
        'controls the vendor lacked or guaranteed fixes.</p>'
        f'<p><strong>Design invariant to examine:</strong> {p.esc(architecture["invariant"])}</p>'
        f'<p><strong>Response boundaries:</strong> {p.esc(architecture["response"])}</p>'
        + decisions
        + f'<p><strong>Validate the design:</strong> {p.esc(architecture["validation"])}</p>'
        + f'<p><strong>Evidence still needed:</strong> {p.esc(architecture["open_questions"])}</p></section>')


def render(incident):
    state = incident.get("analysis_status", "unassessed")
    if state != "assessed":
        observed = ""
        if incident.get("source_id") in {"github", "cloudflare"}:
            updates = incident.get("evidence", [])
            first = updates[0]["text"] if updates else "No retained provider update."
            last = updates[-1]["text"] if updates else "No recovery evidence retained."
            observed = (
                '<section class="causal-explanation"><h4>Observed sequence (automated, not AI analysis)</h4>'
                '<ol class="causal-map" aria-label="Observed provider sequence">'
                '<li class="causal-node causal-unknown"><strong>1. Cause not published</strong>'
                '<small>No technical failure chain is inferred.</small></li>'
                f'<li class="causal-node"><strong>2. First retained notice</strong><p>{p.esc(first)}</p></li>'
                f'<li class="causal-node"><strong>3. Latest retained notice</strong><p>{p.esc(last)}</p></li>'
                '</ol><p>These are reporting checkpoints, not a reconstruction of normal '
                'internal topology, exact onset, or root cause. '
                + p.source_link(incident["url"], "Read the provider evidence") + '</p></section>')
        return (f'<p class="analysis-pending">AI learning analysis: {p.esc(state)}. '
                f'{p.esc(incident.get("analysis_note") or "Not scored; a source-reading agent has not reviewed this entry.")}</p>'), observed
    assessment = incident["analysis"]
    d = assessment["dimensions"]
    summary = (
        f'<div class="learning-assessment"><strong>Learning score {incident["learning_score"]}/100'
        f' &middot; {p.esc(incident["learning_judgment"])}</strong>'
        f'<p>Evidence depth {d["detail"]["score"]}/4 &middot; '
        f'Confidence {d["confidence"]["score"]}/4 '
        '<a href="#score-guide">Score definitions</a></p>'
        f'<p>{p.esc(assessment["why"])}</p></div>')
    detail = (
        '<details class="analysis-detail"><summary>Learning assessment and exercise</summary><h4>What you will learn</h4>'
        f'<p>{p.esc(assessment["learn"])}</p><dl>'
        + "".join(f'<dt>{p.esc("Evidence depth" if key == "detail" else key.title())} {value["score"]}/4</dt>'
                  f'<dd>{p.esc(value["rationale"])}</dd>' for key, value in d.items())
        + '</dl><p>AI-assisted source review: ' + p.esc(assessment["model"])
        + ' &middot; ' + p.esc(assessment["analyzed_at"])
        + '. Subjective study priority, not an objective severity rating. '
        'Daily collection checks evidence; it does not run an AI model.</p></details>')
    detail = render_diagram(assessment) + render_architecture(assessment) + detail
    return summary, detail


def check_sources(document, loader=e.fetch_article):
    urls = sorted({source["url"] for value in document["entries"].values()
                   for source in validate(value)["sources"] if source["kind"] == "article"})
    now = p.stamp(datetime.now(timezone.utc))
    result = {"checked_at": now, "sources": {}}
    for index, url in enumerate(urls):
        checked = {"checked_at": now}
        try:
            if index >= MAX_CHECKS:
                raise ValueError("Daily source-check budget reached")
            e.article_url(url)
            checked.update(status="ok", sha256=e.digest(loader(url)))
        except (OSError, ValueError) as error:
            checked.update(status="error", error=f"{type(error).__name__}: {error}")
            print(f"Learning evidence check failed [{url}]: {error}", file=sys.stderr)
        result["sources"][url] = checked
    return result


def main():
    if sys.argv[1:] != ["check"]:
        raise SystemExit("Usage: python -m scripts.learning check")
    document = p.read_json(p.ROOT / "data" / "learning-analysis.json", {"entries": {}})
    result = check_sources(document)
    p.write_json(p.ROOT / "data" / "analysis-checks.json", result)
    print(f'Checked {len(result["sources"])} AI assessment source bodies (no inference).')


if __name__ == "__main__":
    main()
