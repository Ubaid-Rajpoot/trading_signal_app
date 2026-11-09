/* Run with: node --test web/state.test.js */
const test = require("node:test");
const assert = require("node:assert/strict");
const { lifecycleStateOf, displayStateOf } = require("./state.js");

test("an untouched valid setup is active on the board", () => {
  const row = { state: "pending", valid: true, mitigated: false, touched_ms: null };
  assert.equal(lifecycleStateOf(row), "pending");
  assert.equal(displayStateOf(row), "active");
});

test("a filled unresolved setup is shown as mitigated, not active", () => {
  const row = { state: "active", valid: true, mitigated: true, touched_ms: 1234 };
  assert.equal(lifecycleStateOf(row), "active");
  assert.equal(displayStateOf(row), "mitigated");
});

test("touch time repairs old archive JSON that omitted the mitigated flag", () => {
  assert.equal(displayStateOf({ state: "active", touched_ms: 1234 }), "mitigated");
});

test("resolved lifecycle outcomes are never relabelled active", () => {
  for (const state of ["win", "stopped", "breakeven", "expired", "invalidated"]) {
    assert.equal(displayStateOf({ state, mitigated: true }), state);
  }
});

test("a stale API display label cannot override a resolved lifecycle", () => {
  assert.equal(displayStateOf({ state: "win", display_state: "active", mitigated: true }), "win");
  assert.equal(displayStateOf({ state: "active", display_state: "active", mitigated: true }), "mitigated");
});
