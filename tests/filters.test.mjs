import test from "node:test";
import assert from "node:assert/strict";
import { matches } from "../web/app.mjs";

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
