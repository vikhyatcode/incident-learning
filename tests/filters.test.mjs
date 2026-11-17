import test from "node:test";
import assert from "node:assert/strict";
import { matches, compareIncidents } from "../web/app.mjs";

const record = {
  search: "GitHub Database failover", type: "outage", cause: "dependency",
  kind: "historical", date: "2018-10-21",
};

test("search is case-insensitive, trimmed and combined with cause and type", () => {
  assert.equal(matches(record, { q: " DATABASE ", cause: "dependency", type: "outage" }), true);
  assert.equal(matches(record, { q: "database", cause: "security" }), false);
  assert.equal(matches(record, { type: "security" }), false);
  assert.equal(matches(record, { kind: "live" }), false);
  assert.equal(matches(record, {}), true);
});

test("date range is inclusive and excludes out-of-range or reversed bounds", () => {
  assert.equal(matches(record, { from: "2018-10-21", to: "2018-10-21" }), true);
  assert.equal(matches(record, { from: "2018-10-22" }), false);
  assert.equal(matches(record, { to: "2018-10-20" }), false);
  assert.equal(matches(record, { from: "2019-01-01", to: "2018-01-01" }), false);
});

const sortable = (id, orderDate, learningTier = "0", lessonCount = "0") => ({
  ...record, id, orderDate, learningTier, lessonCount,
});

test("latest uses full timestamps and deterministic identity ties without pinning studies", () => {
  const entries = [
    sortable("old-study", "2018-10-21T22:52:00Z", "2", "3"),
    sortable("z", "2026-11-17T10:00:00Z"),
    sortable("earlier", "2026-11-17T09:00:00Z"),
    sortable("a", "2026-11-17T10:00:00Z"),
  ];
  assert.deepEqual(entries.sort(compareIncidents).map(r => r.id),
    ["a", "z", "earlier", "old-study"]);
});

test("most learnings uses current AI scores, not lesson counts; unassessed and stale are unranked", () => {
  const entries = [
    {...sortable("unassessed", "2026-11-17T00:00:00Z", "2", "100"), analysisStatus: "unassessed"},
    {...sortable("stale", "2026-11-16T00:00:00Z"), analysisStatus: "stale", learningScore: "100"},
    {...sortable("low", "2026-11-17T00:00:00Z", "2", "8"), analysisStatus: "assessed", learningScore: "30"},
    {...sortable("old-study", "2018-10-21T00:00:00Z"), analysisStatus: "assessed", learningScore: "90"},
    {...sortable("z", "2026-11-17T00:00:00Z"), analysisStatus: "assessed", learningScore: "90"},
    {...sortable("a", "2026-11-17T00:00:00Z"), analysisStatus: "assessed", learningScore: "90"},
  ];
  assert.deepEqual(entries.sort((a, b) => compareIncidents(a, b, "learnings")).map(r => r.id),
    ["a", "z", "old-study", "low", "unassessed", "stale"]);
  const filtered = entries.filter(r => matches(r, {q: "database", cause: "dependency"}));
  assert.deepEqual(filtered.sort((a, b) => compareIncidents(a, b, "learnings")),
    entries);
  assert.equal(entries.filter(r => matches(r, {type: "security"})).length, 0);
});
