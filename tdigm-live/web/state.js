/* Trader-facing setup status, kept separate from the persisted trade lifecycle.
 *
 * The engine's historical lifecycle names are `pending` before mitigation and
 * `active` after a fill. Those names are useful to the state machine but were
 * misleading on screen: a signal that had already traded into its zone still
 * appeared ACTIVE. This adapter preserves the stored/API lifecycle while making
 * the board say ACTIVE before mitigation and MITIGATED after it.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.TdigmState = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function lifecycleStateOf(row) {
    if (row && row.lifecycle_state) return String(row.lifecycle_state).toLowerCase();
    if (row && row.state) return String(row.state).toLowerCase();
    return row && row.valid === false ? "expired" : "pending";
  }

  function displayStateOf(row) {
    var lifecycle = lifecycleStateOf(row || {});
    var mitigated = !!(row && (row.mitigated || Number(row.touched_ms || 0) > 0));
    if (lifecycle === "pending" || lifecycle === "active" || lifecycle === "open") {
      return mitigated ? "mitigated" : "active";
    }
    // Lifecycle is authoritative for resolved rows. This deliberately ignores a
    // stale display_state left by an old replay, which is how decided rows once
    // appeared ACTIVE even though their stored state said win/stopped.
    if (["win", "stopped", "breakeven", "expired", "invalidated"].includes(lifecycle)) {
      return lifecycle;
    }
    if (row && row.display_state) return String(row.display_state).toLowerCase();
    return lifecycle;
  }

  return { lifecycleStateOf: lifecycleStateOf, displayStateOf: displayStateOf };
});
