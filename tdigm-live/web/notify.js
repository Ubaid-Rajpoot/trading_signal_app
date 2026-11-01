/* The client half of the notification contract.
 *
 * The server already decides what is worth *sending*; this file decides what is
 * worth *showing*, and it is deliberately stricter. It exists because a page
 * that trusts the network is one reconnect away from a wall of boxes: the old
 * client rendered every alert it received, twice (socket + poll), with no cap
 * and no dedupe.
 *
 * Rules, all enforced here as well as on the server:
 *   1. at most ONE toast on screen at a time - a new one replaces the old;
 *   2. never the same event key twice;
 *   3. never two toasts closer than `minGapMs` apart (default 60s);
 *   3b. two *signals* are never merged into one row, even with the same title -
 *       they are different trades and hiding one is losing one;
 *   4. everything else lands in the inbox and moves the unread badge;
 *   5. the event list is bounded, so a long replay cannot grow the page.
 *
 * Pure functions and a factory with an injectable clock - no DOM in this file,
 * which is what makes `web/notify.test.js` possible.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TdigmNotify = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const DEFAULTS = {
    minGapMs: 60_000,   // one toast a minute, at most
    maxEvents: 200,        // the inbox is a glance, not a log file
    seenLimit: 1000,       // keys remembered for dedupe
    mergeWindowMs: 120_000, // only chatter this close together shares a row
    clock: () => Date.now(),
  };

  function createQueue(options) {
    const config = Object.assign({}, DEFAULTS, options || {});
    const seen = new Map();      // key -> time first seen
    const events = [];           // newest first
    let lastToastAt = -Infinity;
    let unread = 0;

    function remember(key, now) {
      seen.set(key, now);
      if (seen.size > config.seenLimit) {
        const drop = seen.size - config.seenLimit;
        let i = 0;
        for (const k of seen.keys()) {
          if (i++ >= drop) break;
          seen.delete(k);
        }
      }
    }

    /** Add one event. Returns {event, toast, unread, reason}. */
    function push(raw, options2) {
      const now = config.clock();
      const event = Object.assign({ time: now, count: 1, severity: "info" }, raw || {});
      const key = event.key || `${event.category || "x"}:${event.title || event.time}`;
      event.key = key;

      // A message we already hold may come back holding a larger count: the
      // server coalesced another repeat into it. That is an update, not a new
      // event, and it is the only case where an already-seen key is rendered.
      const held = events.find((e) => e.key === key);
      if (held) {
        const count = Math.max(held.count || 1, event.count || 1);
        const changed = count !== held.count;
        held.count = count;
        held.time = Math.max(held.time || 0, event.time || 0);
        return { event: held, toast: null, unread, reason: changed ? "merged" : "duplicate" };
      }
      if (seen.has(key)) {
        return { event: null, toast: null, unread, reason: "duplicate" };
      }
      remember(key, now);

      // Merge into an open group by (category, title) - but never for signals.
      // Two different setups on the same instrument share a title, and folding
      // them together would silently hide one of them.
      const window = config.mergeWindowMs;
      const existing = event.category === "signal"
        ? null
        : events.find((e) => e.category === event.category && e.title === event.title
                          && Math.abs(now - (e.time || now)) <= window);
      if (existing) {
        existing.count = (existing.count || 1) + (event.count || 1);
        existing.time = event.time;
        if (event.detail && String(existing.detail || "").indexOf(event.detail) < 0) {
          existing.detail = [existing.detail, event.detail].filter(Boolean).join(" · ").slice(0, 400);
        }
        return { event: existing, toast: null, unread, reason: "merged" };
      }

      events.unshift(event);
      if (events.length > config.maxEvents) events.length = config.maxEvents;
      unread += 1;

      const wantsToast = (options2 && options2.toast) === true;
      const toastable = event.severity === "action";
      const allowed = wantsToast && toastable && now - lastToastAt >= config.minGapMs;
      if (allowed) lastToastAt = now;
      let reason = "inbox";
      if (allowed) reason = "toast";
      else if (wantsToast && toastable) reason = "rate-limited";
      return { event, toast: allowed ? event : null, unread, reason };
    }

    function markRead() {
      const cleared = unread;
      unread = 0;
      return cleared;
    }

    function list(limit) {
      return events.slice(0, limit || events.length);
    }

    /** Change the minimum gap without losing what the inbox already holds. */
    function setMinGap(ms) {
      config.minGapMs = Math.max(0, Number(ms) || 0);
    }

    function reset() {
      seen.clear();
      events.length = 0;
      unread = 0;
      lastToastAt = -Infinity;
    }

    return {
      push,
      markRead,
      setMinGap,
      list,
      reset,
      get unread() { return unread; },
      get lastToastAt() { return lastToastAt; },
    };
  }

  /** Human wording for one event, used by both the toast and the inbox. */
  function describe(event) {
    if (!event) return "";
    const bits = [event.title || ""];
    if (event.detail) bits.push(event.detail);
    if (event.count > 1) bits.push(`×${event.count}`);
    return bits.filter(Boolean).join(" · ");
  }

  return { createQueue, describe, DEFAULTS };
});
