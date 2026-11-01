/* TDIGM v7 page.
 *
 * Three panes, one table, one chart, and exactly one toast slot. The rules this
 * file obeys, in order of importance:
 *
 *   1. Nothing on this page may ever cover the page. Toasts come from
 *      `TdigmNotify` (see /notify.js), which shows one at a time and at most one
 *      a minute; a server message that is not actionable never becomes a toast,
 *      no matter how many of them arrive.
 *   2. There is one source of truth for the table: the archive in SQLite, read
 *      from /api/signals, overlaid with the live snapshot. A restart re-reads it,
 *      so a stored signal is still on the page after the process is gone.
 *   3. Every number is either measured here or labelled as what it is. Nothing
 *      is invented to fill a column.
 *   4. If the socket dies the page says so once, in the banner, and keeps
 *      working over HTTP - it does not toast every retry.
 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var NF = window.TdigmNotify;
  var quiet = localStorage.getItem("tdigm.quiet") !== "0";

  // ------------------------------------------------------------------ model
  var model = {
    rows: new Map(),        // key -> row (archive + live overlay)
    live: new Set(),        // keys the engine currently holds
    order: [],              // keys, newest first (recomputed on render)
    selected: null,         // key
    filters: { search: "", tfs: new Set(), side: "both", grades: new Set(), sessions: new Set(), state: "all", sort: "new" },
    state: null,            // last snapshot
    health: null,
    archive: {},
    results: {},
    counts: {},
    chartKey: "",
    chartBusy: false,
    wsOk: false,
    lastMessage: 0,
    toastTimer: 0,
    chartTimer: 0,
    medianRr: null,
  };

  var queue = NF.createQueue({ minGapMs: quiet ? 60_000 : 8_000 });
  var toastQueue = queue;

  // ------------------------------------------------------------------ format
  function num(v, digits) {
    if (v === null || v === undefined || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    var abs = Math.abs(n);
    if (digits === undefined) {
      if (abs >= 1000) digits = 2;
      else if (abs >= 1) digits = 3;
      else if (abs >= 0.01) digits = 5;
      else digits = 7;
    }
    return n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits });
  }

  function clock(ms) {
    if (!ms) return "—";
    var d = new Date(Number(ms));
    if (isNaN(d.getTime())) return "—";
    var pad = function (x) { return String(x).padStart(2, "0"); };
    return pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  function ago(ms) {
    var s = Math.max(0, Math.round((Date.now() - Number(ms || 0)) / 1000));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return Math.round(s / 3600) + "h";
    return Math.round(s / 86400) + "d";
  }

  function sideOf(row) { return String(row.side || "").toUpperCase() === "SHORT" ? "short" : "long"; }

  function stateOf(row) {
    if (row.state) return String(row.state).toLowerCase();
    return row.valid === false ? "expired" : "open";
  }

  function normaliseRow(raw) {
    return {
      key: raw.key || (raw.symbol + "|" + raw.timeframe + "|" + (raw.created_ms || raw.time)),
      symbol: raw.symbol || "",
      timeframe: raw.timeframe || "",
      side: String(raw.side || "").toUpperCase(),
      grade: raw.grade || "",
      state: stateOf(raw),
      valid: raw.valid !== false,
      entry: raw.entry,
      stop: raw.stop,
      target: raw.target,
      rr: raw.rr,
      r_now: raw.r_now,
      score: raw.score,
      session: raw.session || "",
      created_ms: Number(raw.created_ms || raw.time || 0),
      zone: raw.zone || null,
      swept_level: raw.swept_level,
      bos_level: raw.bos_level,
      break_kind: raw.break_kind,
      hvb_side: raw.hvb_side,
      volume_ratio: raw.volume_ratio,
      reasons: raw.reasons || raw.confluences || [],
      evidence: raw.evidence || [],
      trap: raw.trap || null,
      shift: raw.shift || null,
      ac: raw.ac || raw.imbalance || null,
      fee_r: raw.fee_r,
      htf_bias: raw.htf_bias,
      premium: raw.premium,
      stored: !!raw.stored,
      grade_reasons: raw.grade_reasons || [],
      missing: raw.missing || [],
      raw: raw,
    };
  }

  function absorbRows(list, live) {
    if (!Array.isArray(list)) return 0;
    var added = 0;
    for (var i = 0; i < list.length; i++) {
      var row = normaliseRow(list[i] || {});
      if (!row.key || !row.symbol) continue;
      if (live) {
        model.live.add(row.key);
        model.rows.set(row.key, Object.assign({}, model.rows.get(row.key) || {}, row));
      } else if (!model.rows.has(row.key)) {
        model.rows.set(row.key, row);
        added++;
      } else if (!model.live.has(row.key)) {
        // the archive is the record; the live copy wins while the engine holds it
        model.rows.set(row.key, Object.assign({}, model.rows.get(row.key), row));
      }
    }
    return added;
  }

  // ------------------------------------------------------------------ filters
  function matches(row) {
    var f = model.filters;
    if (f.side !== "both" && sideOf(row) !== f.side) return false;
    if (f.tfs.size && !f.tfs.has(row.timeframe)) return false;
    if (f.grades.size && !f.grades.has(row.grade || "?")) return false;
    if (f.sessions.size && !f.sessions.has(row.session || "?")) return false;
    if (f.state === "open" && !row.valid) return false;
    if (f.state !== "all" && f.state !== "open" && row.state !== f.state) return false;
    if (f.search) {
      var hay = (row.symbol + " " + row.timeframe + " " + row.side).toUpperCase();
      if (hay.indexOf(f.search.toUpperCase()) < 0) return false;
    }
    return true;
  }

  function sorted() {
    var out = [];
    model.rows.forEach(function (row) { if (matches(row)) out.push(row); });
    var by = model.filters.sort;
    out.sort(function (a, b) {
      if (by === "rr") return (Number(b.rr) || 0) - (Number(a.rr) || 0);
      if (by === "symbol") return a.symbol.localeCompare(b.symbol) || b.created_ms - a.created_ms;
      if (by === "moving") return Math.abs(Number(b.r_now) || 0) - Math.abs(Number(a.r_now) || 0);
      return b.created_ms - a.created_ms;
    });
    return out;
  }

  // ------------------------------------------------------------------- table
  function cell(html, cls) {
    var td = document.createElement("td");
    if (cls) td.className = cls;
    td.innerHTML = html;
    return td;
  }

  function renderTable() {
    var rows = sorted();
    var body = $("rows");
    var frag = document.createDocumentFragment();
    var limit = Math.min(rows.length, 400);
    for (var i = 0; i < limit; i++) {
      var r = rows[i];
      var tr = document.createElement("tr");
      tr.dataset.key = r.key;
      if (r.key === model.selected) tr.className = "sel";
      tr.appendChild(cell(clock(r.created_ms), "c-time"));
      tr.appendChild(cell(escapeHtml(r.symbol), "c-sym"));
      tr.appendChild(cell(escapeHtml(r.timeframe), "c-tf"));
      tr.appendChild(cell(sideOf(r), "c-side side " + sideOf(r)));
      tr.appendChild(cell(escapeHtml(r.grade || "—"), "c-grade grade " + escapeHtml(r.grade || "")));
      tr.appendChild(cell(num(r.entry), "c-num"));
      tr.appendChild(cell(num(r.stop), "c-num"));
      tr.appendChild(cell(num(r.target), "c-num"));
      tr.appendChild(cell(r.rr ? "1:" + Number(r.rr).toFixed(1) : "—", "c-num"));
      var now = r.r_now;
      tr.appendChild(cell(now === null || now === undefined ? "—" : (Number(now) > 0 ? "+" : "") + Number(now).toFixed(2) + "R",
        "c-num now " + (Number(now) > 0 ? "pos" : Number(now) < 0 ? "neg" : "")));
      tr.appendChild(cell(stateOf(r), "c-state state " + stateOf(r)));
      frag.appendChild(tr);
    }
    body.replaceChildren(frag);
    $("list-count").textContent = rows.length;
    $("empty").hidden = rows.length > 0;
    if (!rows.length) {
      $("empty-why").textContent = model.rows.size
        ? "No stored signal matches these filters. Clear them to see all " + model.rows.size + "."
        : (model.state ? "The engine has not confirmed a setup yet. It is still building its liquidity map." : "Waiting for the engine…");
    }
    var meta = [];
    if (model.counts && model.counts.total) meta.push(model.counts.total + " in the archive");
    if (model.live.size) meta.push(model.live.size + " live");
    $("list-meta").textContent = meta.join(" · ") || "waiting for the engine…";
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ------------------------------------------------------------------ facets
  function chipRow(host, values, set, label) {
    host.replaceChildren();
    values.forEach(function (value) {
      var b = document.createElement("button");
      b.className = "chip" + (set.has(value) ? " on" : "");
      b.textContent = label ? label(value) : value;
      b.onclick = function () {
        if (set.has(value)) set.delete(value); else set.add(value);
        chipRow(host, values, set, label);
        renderTable();
      };
      host.appendChild(b);
    });
  }

  function buildFacets() {
    var tfs = (model.state && model.state.timeframes) || [];
    if (!tfs.length) {
      var seen = new Set();
      model.rows.forEach(function (r) { if (r.timeframe) seen.add(r.timeframe); });
      tfs = Array.from(seen);
    }
    tfs.sort(function (a, b) { return rankTf(a) - rankTf(b) || a.localeCompare(b); });
    chipRow($("chips-tf"), tfs, model.filters.tfs);

    var grades = ["A", "B"];
    chipRow($("chips-grade"), grades, model.filters.grades);

    var sessions = [];
    var ss = new Set();
    model.rows.forEach(function (r) { if (r.session) ss.add(r.session); });
    sessions = Array.from(ss).sort();
    $("chips-session").parentElement.hidden = sessions.length === 0;
    chipRow($("chips-session"), sessions, model.filters.sessions);
  }

  function rankTf(tf) {
    var table = { "1m": 1, "3m": 2, "5m": 3, "15m": 4, "30m": 5, "1h": 6, "2h": 7, "4h": 8, "6h": 9, "12h": 10, "1d": 11, "1w": 12 };
    return table[tf] || 50;
  }

  // ------------------------------------------------------------------ header
  function renderHeader() {
    var s = model.state;
    var pill = $("source-pill");
    var label = "connecting";
    pill.className = "pill";
    if (s) {
      var source = s.feed && s.feed.source ? s.feed.source : s.source;
      var status = s.feed && s.feed.status ? s.feed.status : (s.feed && s.feed.connected ? "live" : "");
      label = [source || "engine", status].filter(Boolean).join(" · ");
      pill.className = "pill " + (source === "replay" ? "replay" : model.wsOk ? "live" : "down");
    } else if (!model.wsOk) {
      pill.className = "pill down";
    }
    $("source-text").textContent = label;
    if (s) {
      $("strategy-tag").textContent = s.strategy === "book" ? "book engine" : "TDIGM setup";
      $("stat-symbols").textContent = (s.symbols ? s.symbols.length : 0) + " symbols · " + (s.warming || 0) + " warming" + (s.failed ? " · " + s.failed + " failed" : "");
      var tf = s.timeframes && s.timeframes.length ? s.timeframes.join(" ") : "";
      $("stat-candle").textContent = (s.candles_seen !== null && s.candles_seen !== undefined ? num(s.candles_seen, 0) + " candles" : "") + (tf ? " · " + tf : "");
    }
    if (model.health) {
      var n = model.health.notifications || {};
      var noise = (n.filtered || 0) + (n.suppressed || 0);
      $("status-noise").textContent = noise + " notice(s) hidden";
      $("status-noise").title = (n.filtered || 0) + " rejected setup notices and " + (n.suppressed || 0)
        + " duplicate/chatter notices hidden. No market frames or confirmed signals are dropped here.";
      $("status-counts").textContent = (model.health.signals || 0) + " signals · " + (model.health.stored || 0)
        + " stored · " + (model.health.setups_valid || 0) + " valid now · "
        + (model.health.setups_unmitigated || 0) + " unmitigated";
      renderFunnel();
      $("status-feed").textContent = "feed: " + (model.health.feed || "—") + " · " + num(model.health.frames, 0) + " frames";
    }
    $("status-memory").textContent = s && s.uptime ? "up " + ago(Date.now() - s.uptime * 1000) : "";
  }

  function renderStats() {
    var res = model.results || {};
    var all = res.all || {};
    var counts = model.counts || {};
    var byState = counts.by_state || {};

    var verdict = all.verdict || "not enough trades";
    var word = verdict === "not proven" ? "not proven"
      : verdict === "not enough trades" ? "too few trades" : verdict;
    var cls = verdict === "positive" ? "positive" : verdict === "negative" ? "negative" : "unproven";
    var badge = $("record-verdict");
    badge.className = "verdict " + cls;
    badge.textContent = word;
    badge.title = "95% interval on R per trade: "
      + (all.ci_low === undefined ? "—" : all.ci_low + " to " + all.ci_high)
      + ". '" + word + "' means exactly that - nothing about it is rounded in the scanner's favour.";

    function row(label, value, title) {
      return '<div' + (title ? ' title="' + escapeHtml(title) + '"' : '') + '><span class="muted">'
        + escapeHtml(label) + "</span><b>" + escapeHtml(value) + "</b></div>";
    }
    var rows = [];
    rows.push(row("signals stored", num(counts.total, 0), "every plan ever recorded, across restarts"));
    rows.push(row("decided", num(all.decided, 0), "closed at the draw, the stop or break-even"));
    rows.push(row("win rate", all.win_rate == null ? "—" : (all.win_rate * 100).toFixed(1) + "%",
      "wins / decided. At the median 1:" + (model.medianRr || "?") + " a plan needs "
      + (model.medianRr ? Math.round(100 / (1 + model.medianRr)) : "?") + "% to break even before fees."));
    var rpt = all.r_per_trade;
    rows.push(row("R / trade", rpt == null ? "—" : (rpt > 0 ? "+" : "") + Number(rpt).toFixed(3),
      "measured on the stored rows at the fixed 2R convention, net of nothing else"));
    rows.push(row("95% interval", all.ci_low == null ? "—"
      : (all.ci_low > 0 ? "+" : "") + Number(all.ci_low).toFixed(2) + " … "
        + (all.ci_high > 0 ? "+" : "") + Number(all.ci_high).toFixed(2),
      "if this includes zero, the average is indistinguishable from luck"));
    rows.push(row("open now", num(byState.active || 0, 0), "live and not yet resolved"));
    var byTf = (res.by_timeframe) || {};
    var names = Object.keys(byTf).sort();
    if (names.length) {
      rows.push('<div class="muted" style="font-size:10px;letter-spacing:.06em;margin-top:4px">BY TIMEFRAME</div>');
      names.forEach(function (tf) {
        var g = byTf[tf];
        if (!g || !g.scored) return;
        var tone = g.verdict === "positive" ? "pos" : g.verdict === "negative" ? "neg" : "";
        rows.push('<div title="' + escapeHtml(g.verdict) + ' · 95% interval '
          + g.ci_low + " … " + g.ci_high + '"><span class="muted">' + escapeHtml(tf) + " · "
          + g.scored + "</span><b class='" + tone + "'>"
          + (g.r_per_trade > 0 ? "+" : "") + Number(g.r_per_trade).toFixed(2) + "R</b></div>");
      });
    }
    var stats = $("record-body");
    if (stats) stats.innerHTML = rows.join("");
    var note = $("record-note");
    if (note) {
      var n = all.scored || 0;
      note.innerHTML = n
        ? "<b>" + n + "</b> scored trade(s). A track record this size carries a standard error of about "
          + (all.se == null ? "?" : Number(all.se).toFixed(2)) + "R per trade, so the interval above is the honest"
          + " reading — not the average."
        : "No trade has closed yet. The record appears as plans resolve; nothing is projected.";
    }
  }

  // ------------------------------------------------------------------- story
  function renderStory(row) {
    var host = $("story");
    if (!row) { host.innerHTML = ""; return; }
    var bits = [];
    var chain = [];
    if (row.trap) chain.push("trap " + (row.trap.side || "") + " @ " + num(row.trap.level));
    if (row.ac) chain.push("imbalance " + num(row.ac.bottom) + "–" + num(row.ac.top));
    if (row.shift) chain.push("shift " + (row.shift.kind || "MS") + " @ " + num(row.shift.level));
    if (row.target) chain.push("draw @ " + num(row.target));
    if (chain.length) bits.push('<div class="chain">' + escapeHtml(chain.join("  →  ")) + "</div>");
    var nums = [];
    ["entry", "stop", "target"].forEach(function (k) {
      if (row[k] !== null && row[k] !== undefined) nums.push(k + " <b>" + escapeHtml(num(row[k])) + "</b>");
    });
    if (row.rr) nums.push("planned <b>1:" + Number(row.rr).toFixed(1) + "</b>");
    if (row.r_now !== null && row.r_now !== undefined) nums.push("now <b>" + (Number(row.r_now) > 0 ? "+" : "") + Number(row.r_now).toFixed(2) + "R</b>");
    if (row.fee_r !== null && row.fee_r !== undefined) nums.push("fee <b>" + Number(row.fee_r).toFixed(2) + "R</b>");
    if (row.score !== null && row.score !== undefined) nums.push("score <b>" + escapeHtml(row.score) + "</b>");
    if (nums.length) bits.push('<div class="nums">' + nums.join("") + "</div>");
    if (row.grade) {
      var held = (row.grade_reasons || []).length;
      var miss = (row.missing || []);
      var story = '<div class="nums" title="A description of the story, not a quality rank: '
        + 'measured on the archive, plans with the fullest stories have not done better than incomplete ones.">'
        + "<span>story <b>" + escapeHtml(row.grade) + "</b> · " + held + " of " + (held + miss.length)
        + " structural checks";
      if (miss.length) story += ' · missing: <b>' + escapeHtml(miss.join(", ")) + "</b>";
      story += "</span></div>";
      bits.push(story);
    }
    var bucket = bucketRecord(row);
    if (bucket) {
      bits.push('<div class="nums" title="Every closed trade in this instrument-timeframe-side bucket, '
        + 'at the fixed 2R convention. The interval is what the sample actually supports.">'
        + "<span>" + escapeHtml(row.timeframe) + " " + escapeHtml(sideOf(row)) + " so far: <b>"
        + bucket.n + "</b> closed · <b>" + (bucket.r > 0 ? "+" : "") + Number(bucket.r).toFixed(2) + "R</b>"
        + " each · 95% " + (bucket.ci_low > 0 ? "+" : "") + Number(bucket.ci_low).toFixed(2)
        + "…" + (bucket.ci_high > 0 ? "+" : "") + Number(bucket.ci_high).toFixed(2)
        + " <b>" + escapeHtml(bucket.verdict) + "</b></span></div>");
    }
    var why = (row.reasons && row.reasons.length ? row.reasons : row.evidence) || [];
    if (why.length) bits.push('<div class="why">' + why.slice(0, 8).map(function (w) { return "<div>" + escapeHtml(w) + "</div>"; }).join("") + "</div>");
    if (!bits.length) bits.push('<div class="muted">This setup carries no narrative: it came from the archive before the current engine, so there is nothing to explain. Its numbers above are the record.</div>');
    host.innerHTML = bits.join("");
  }

  /** The archive's own record for this signal's bucket (timeframe x side). */
  function bucketRecord(row) {
    var by = (model.results && model.results.by_timeframe) || {};
    var group = by[row.timeframe];
    if (!group) return null;
    var all = (model.results && model.results.by_side) || {};
    var sideBucket = all[row.side];
    if (!sideBucket) return group.verdict ? group : null;
    // the tightest honest thing we can say: how this timeframe has done overall
    return group.verdict ? group : sideBucket;
  }

  // ----------------------------------------------------- what it is watching
  function renderWatch(data) {
    var rows = (data && data.watching) || [];
    var host = $("watch-list");
    var box = $("watch");
    if (!rows.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    var shown = rows.slice(0, 12);
    host.innerHTML = shown.map(function (w) {
      var st, cls, what, need;
      if (w.mode === "sweep") {
        var s = w.structure || {};
        st = "swept";
        cls = "st-trap";
        what = (s.swept_side === "high" ? "took high " : "took low ") + num(s.swept_level);
        need = "body close " + (s.watching_side === "low" ? "below " : "above ") + num(s.watching)
          + " with matching HVB";
      } else {
        st = w.armed ? "armed" : "trap";
        cls = w.armed ? "st-armed" : "st-trap";
        what = w.armed
          ? "gap " + num(w.armed.bottom) + "–" + num(w.armed.top)
            + " · " + Math.round((w.armed.fill || 0) * 100) + "% filled"
          : (w.trap.side === "high" ? "took high " : "took low ") + num(w.trap.level) + " (" + w.trap.name + ")";
        need = w.armed ? "needs the reverse shift" : "needs a displacement";
      }
      return '<div class="watch-row"><span class="sym">' + escapeHtml(w.symbol) + "</span>"
        + "<span>" + escapeHtml(w.timeframe) + "</span>"
        + '<span class="' + cls + '">' + st + "</span>"
        + '<span class="need">' + escapeHtml(what) + " — " + escapeHtml(need) + "</span></div>";
    }).join("");
    $("watch-count").textContent = rows.length + " tracked";
  }

  function refreshWatch() {
    fetch("/api/watch?limit=60")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderWatch(data);
        model.funnel = data.funnel || null;
        if (model.funnel) renderFunnel();
      })
      .catch(function () {});
  }

  function renderFunnel() {
    var f = model.funnel;
    if (!f) return;
    var drops = f.drops || {};
    var worst = Object.keys(drops).sort(function (a, b) { return drops[b] - drops[a]; })[0];
    if (f.mode === "sweep") {
      $("status-funnel").textContent = (f.sweeps || 0) + " sweeps → " + (f.setups || 0)
        + " body breaks → " + (f.plans || 0) + " TDIGM signals"
        + (worst ? " · top rejection: " + worst + " (" + drops[worst] + ")" : "");
    } else {
      $("status-funnel").textContent = (f.traps || 0) + " traps → " + (f.displacements || 0)
        + " displacements → " + (f.plans || 0) + " plans"
        + (worst ? " · mostly refused: " + worst + " (" + drops[worst] + ")" : "");
    }
  }

  // ------------------------------------------------------------------- chart
  function selectRow(row) {
    model.selected = row ? row.key : null;
    Array.prototype.forEach.call(document.querySelectorAll("tr.sel"), function (tr) { tr.classList.remove("sel"); });
    var tr = row ? document.querySelector('tr[data-key="' + CSS.escape(row.key) + '"]') : null;
    if (tr) tr.classList.add("sel");
    if (!row) return;
    $("chart-title").textContent = row.symbol + " " + row.timeframe + " · " + sideOf(row);
    $("chart-sub").textContent = clock(row.created_ms) + " UTC · " + stateOf(row) + (row.grade ? " · grade " + row.grade : "");
    renderStory(row);
    loadChart(row.symbol, row.timeframe);
  }

  function loadChart(symbol, timeframe) {
    var key = symbol + "|" + timeframe;
    model.chartKey = key;
    model.chartBusy = true;
    fetch("/api/chart?symbol=" + encodeURIComponent(symbol) + "&timeframe=" + encodeURIComponent(timeframe) + "&bars=180")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        model.chartBusy = false;
        if (model.chartKey !== key) return;
        drawChart(data);
      })
      .catch(function () { model.chartBusy = false; });
  }

  function drawChart(data) {
    var canvas = $("chart");
    var wrap = canvas.parentElement;
    var dpr = window.devicePixelRatio || 1;
    var w = wrap.clientWidth || 600;
    var h = wrap.clientHeight || 420;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    var candles = (data && data.candles) || [];
    $("chart-hint").hidden = candles.length > 0;
    if (!candles.length) {
      $("chart-hint").hidden = false;
      $("chart-hint").textContent = "No candles yet for " + (data && data.symbol || "") + " " + (data && data.timeframe || "") + " — the instrument is still warming.";
      return;
    }

    var pads = { l: 8, r: 74, t: 14, b: 22 };
    var plotW = w - pads.l - pads.r;
    var plotH = h - pads.t - pads.b;

    // levels that belong on the price axis: pools, the setup, the trade
    var levels = [];
    var pools = data.pools || [];
    pools.forEach(function (p) {
      if (p && (p.level !== undefined) && p.level !== null) levels.push(Number(p.level));
    });
    var setups = data.setups || [];
    setups.forEach(function (s) {
      ["entry", "stop", "target"].forEach(function (k) { if (s[k] !== null && s[k] !== undefined) levels.push(Number(s[k])); });
    });
    var hi = -Infinity, lo = Infinity;
    candles.forEach(function (c) { hi = Math.max(hi, c.h); lo = Math.min(lo, c.l); });
    levels.forEach(function (v) { if (isFinite(v) && v > 0) { hi = Math.max(hi, v); lo = Math.min(lo, v); } });
    var span = hi - lo || 1;
    hi += span * 0.03; lo -= span * 0.03; span = hi - lo;

    function x(i) { return pads.l + (i + 0.5) * (plotW / candles.length); }
    function y(v) { return pads.t + (hi - v) / span * plotH; }
    var cw = Math.max(1.5, (plotW / candles.length) * 0.66);

    // grid: three price lines, labelled on the right
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "middle";
    for (var g = 1; g <= 3; g++) {
      var val = lo + span * (g / 4);
      ctx.strokeStyle = "#161d27";
      ctx.beginPath(); ctx.moveTo(pads.l, y(val)); ctx.lineTo(pads.l + plotW, y(val)); ctx.stroke();
      ctx.fillStyle = "#5b6678";
      ctx.fillText(num(val), pads.l + plotW + 8, y(val));
    }

    // zones the setups came from
    setups.forEach(function (s) {
      var z = s.zone || {};
      if (z.high === undefined || z.low === undefined) return;
      ctx.fillStyle = (String(s.side).toUpperCase() === "SHORT") ? "rgba(255,92,122,.08)" : "rgba(46,230,168,.08)";
      ctx.fillRect(pads.l, y(z.high), plotW, Math.max(1.5, y(z.low) - y(z.high)));
    });

    // candles
    candles.forEach(function (c, i) {
      var up = c.c >= c.o;
      ctx.strokeStyle = up ? "#1c8f69" : "#a83b4f";
      ctx.fillStyle = up ? "#2ee6a8" : "#ff5c7a";
      ctx.beginPath();
      ctx.moveTo(x(i), y(c.h)); ctx.lineTo(x(i), y(c.l)); ctx.stroke();
      var top = y(Math.max(c.o, c.c));
      var bottom = y(Math.min(c.o, c.c));
      ctx.fillRect(x(i) - cw / 2, top, cw, Math.max(1, bottom - top));
    });

    // structure breaks and the TDIGM centre cross, at the candle they printed on
    var index = {};
    candles.forEach(function (c, i) { index[c.t] = i; });
    var markers = data.markers || {};
    (markers.bos || []).forEach(function (m) {
      var i = index[m.t];
      if (i === undefined) return;
      ctx.strokeStyle = m.dir > 0 ? "rgba(46,230,168,.5)" : "rgba(255,92,122,.5)";
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x(i), y(m.level)); ctx.lineTo(pads.l + plotW, y(m.level)); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = m.dir > 0 ? "#2ee6a8" : "#ff5c7a";
      ctx.beginPath();
      var yy = y(candles[i].h) - 6;
      ctx.moveTo(x(i), yy); ctx.lineTo(x(i) - 4, yy - 6); ctx.lineTo(x(i) + 4, yy - 6);
      ctx.closePath(); ctx.fill();
    });
    (markers.hvb || []).forEach(function (m) {
      var i = index[m.t];
      if (i === undefined) return;
      ctx.fillStyle = "rgba(103,169,255,.85)";
      ctx.beginPath();
      ctx.arc(x(i), y(m.side === "up" ? candles[i].l : candles[i].h) + (m.side === "up" ? 6 : -6), 2.6, 0, Math.PI * 2);
      ctx.fill();
    });

    // the liquidity map: dotted lines, labelled with the pool's name
    pools.slice(-14).forEach(function (p) {
      var level = Number(p.level);
      if (!isFinite(level)) return;
      ctx.strokeStyle = "rgba(133,146,166,.22)";
      ctx.setLineDash([2, 5]);
      ctx.beginPath(); ctx.moveTo(pads.l, y(level)); ctx.lineTo(pads.l + plotW, y(level)); ctx.stroke();
      ctx.setLineDash([]);
      var label = String(p.kind || p.name || p.tier || "").slice(0, 14);
      if (label) {
        ctx.fillStyle = "#5b6678";
        ctx.fillText(label, pads.l + 4, y(level) - 6);
      }
    });

    // the trade: entry solid, stop dashed red, draw dashed green
    setups.slice(-6).forEach(function (s) {
      var short = String(s.side).toUpperCase() === "SHORT";
      var colour = short ? "#ff5c7a" : "#2ee6a8";
      if (s.entry !== undefined && s.entry !== null) {
        ctx.strokeStyle = colour; ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(pads.l, y(s.entry)); ctx.lineTo(pads.l + plotW, y(s.entry)); ctx.stroke();
      }
      if (s.stop !== undefined && s.stop !== null) {
        ctx.strokeStyle = "#a83b4f"; ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(pads.l, y(s.stop)); ctx.lineTo(pads.l + plotW, y(s.stop)); ctx.stroke();
        ctx.setLineDash([]);
      }
      if (s.target !== undefined && s.target !== null) {
        ctx.strokeStyle = "#1c8f69"; ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(pads.l, y(s.target)); ctx.lineTo(pads.l + plotW, y(s.target)); ctx.stroke();
        ctx.setLineDash([]);
      }
    });
    ctx.lineWidth = 1;

    // last price, in the axis gutter
    var last = candles[candles.length - 1];
    ctx.fillStyle = "#dfe6f2";
    ctx.fillRect(pads.l + plotW + 3, y(last.c) - 8, pads.r - 8, 16);
    ctx.fillStyle = "#0b0e13";
    ctx.fillText(num(last.c), pads.l + plotW + 7, y(last.c));

    $("chart-hint").hidden = true;
    $("chart-tf").textContent = (data.bars || candles.length) + " bars" + (data.warm ? "" : " · still warming");
  }

  // ------------------------------------------------------- notifications :: ui
  function showToast(event) {
    var box = $("toast");
    clearTimeout(model.toastTimer);
    $("toast-title").textContent = event.title || "signal";
    $("toast-detail").textContent = NF.describe(event);
    box.hidden = false;
    box.dataset.key = event.key || "";
    model.toastTimer = setTimeout(function () { box.hidden = true; }, 7000);
  }

  function renderInbox() {
    var host = $("inbox-list");
    var events = toastQueue.list(80);
    if (!events.length) {
      host.innerHTML = '<div class="inbox-empty">Nothing yet. Only grade A signals and real errors arrive here — everything else is counted, not shown.</div>';
    } else {
      host.innerHTML = events.map(function (e) {
        var cls = "inbox-item " + (e.severity || "info");
        var when = e.time ? ago(e.time) + " ago" : "";
        var title = e.title || e.category || "";
        var detail = [e.detail, e.count > 1 ? "×" + e.count : ""].filter(Boolean).join(" · ");
        return '<div class="' + cls + '"><span class="bullet"></span><div><div class="t">' +
          escapeHtml(title) + (e.grade ? ' <span class="muted">· ' + escapeHtml(e.grade) + "</span>" : "") +
          '</div><div class="d">' + escapeHtml(detail) + '</div></div><div class="when">' + escapeHtml(when) + "</div></div>";
      }).join("");
    }
    var unread = toastQueue.unread;
    var badge = $("inbox-badge");
    badge.hidden = unread <= 0;
    badge.textContent = unread > 99 ? "99+" : String(unread);
    $("btn-inbox").classList.toggle("quiet", unread <= 0);
  }

  function pushEvent(event, wantToast) {
    if (!event) return;
    var result = toastQueue.push(event, { toast: !!wantToast });
    if (!result.event) return;
    renderInbox();
    if (result.toast) showToast(result.event);
  }

  function loadNotifications() {
    fetch("/api/notifications?limit=120")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.events || []).slice().reverse().forEach(function (event) {
          // `toast:false` on the replay of history: the page was just opened,
          // it must not fire a toast for something that happened ten minutes ago
          pushEvent(event, false);
        });
      })
      .catch(function () {});
  }

  // ------------------------------------------------------------ state & poll
  function applyState(snapshot) {
    if (!snapshot) return;
    model.state = snapshot;
    var added = absorbRows(snapshot.signals, true);
    if (snapshot.archive) model.archive = snapshot.archive;
    renderHeader();
    renderTable();
    if (added) buildFacets();
    if (!model.selected) {
      var first = sorted()[0];
      if (first) selectRow(first);
    } else {
      var row = model.rows.get(model.selected);
      if (row) {
        renderStory(row);
        // the chart follows the symbol when its candles move on
        if (model.chartKey !== row.symbol + "|" + row.timeframe) loadChart(row.symbol, row.timeframe);
      }
    }
  }

  function refreshArchive() {
    fetch("/api/signals?limit=800")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var added = absorbRows(data.signals, false);
        model.counts = data.counts || {};
        model.results = data.results || {};
        var rrs = (data.signals || []).map(function (r) { return Number(r.rr); })
          .filter(function (v) { return isFinite(v) && v > 0; }).sort(function (a, b) { return a - b; });
        if (rrs.length) model.medianRr = rrs[Math.floor(rrs.length / 2)].toFixed(1);
        renderStats();
        renderTable();
        if (added) buildFacets();
        if (!model.selected) {
          var first = sorted()[0];
          if (first) selectRow(first);
        }
      })
      .catch(function () {});
  }

  function refreshHealth() {
    fetch("/api/health")
      .then(function (r) { return r.json(); })
      .then(function (data) { model.health = data; renderHeader(); })
      .catch(function () {});
  }

  var archiveTimer = 0;
  function soonArchive() {
    clearTimeout(archiveTimer);
    archiveTimer = setTimeout(refreshArchive, 1200);
  }

  // ------------------------------------------------------------------- alarm
  function banner(text) {
    var el = $("banner");
    if (!text) { el.hidden = true; el.textContent = ""; return; }
    if (el.hidden || el.textContent !== text) { el.textContent = text; el.hidden = false; }
  }

  function checkAlarm() {
    if (!model.state) return;
    var h = model.health || {};
    var age = model.lastMessage ? (Date.now() - model.lastMessage) / 1000 : null;
    if (h.feed === "stopped" || (h.failed && h.failed > 0 && h.symbols === 0)) {
      banner("The feed stopped. The page is still reading the archive, but no new candles are arriving. Check the server log.");
    } else if (!model.wsOk && age !== null && age > 20) {
      banner("Socket disconnected — falling back to HTTP polling, so the table may lag by a few seconds. It will reconnect by itself.");
    } else {
      banner("");
    }
  }

  // ---------------------------------------------------------------- websocket
  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/ws";
    var ws;
    var retry = 0;
    var pingTimer = 0;

    function open() {
      ws = new WebSocket(url);
      ws.onopen = function () {
        model.wsOk = true;
        retry = 0;
        model.lastMessage = Date.now();
        checkAlarm();
        clearInterval(pingTimer);
        pingTimer = setInterval(function () {
          try { ws.send("ping"); } catch (e) { /* the close handler takes over */ }
        }, 25000);
      };
      ws.onmessage = function (ev) {
        model.lastMessage = Date.now();
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (!msg || !msg.type) return;
        if (msg.type === "state") { applyState(msg); refreshHealth(); }
        else if (msg.type === "notify") {
          pushEvent(msg.event, msg.toast === true);
          if (msg.event && msg.event.category === "signal") soonArchive();
        } else if (msg.type === "setup") {
          soonArchive();
        }
        // `log`, `alert`, `filtered` are *only* ever handled by the server's
        // policy. They are never rendered here, which is what makes the page
        // incapable of cluttering itself.
      };
      ws.onclose = function () {
        model.wsOk = false;
        clearInterval(pingTimer);
        checkAlarm();
        retry = Math.min(retry + 1, 8);
        setTimeout(open, Math.min(1000 * Math.pow(1.7, retry), 20000));
      };
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }
    open();
  }

  function poll() {
    fetch("/api/state")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        model.lastMessage = model.lastMessage || Date.now();
        applyState(data);
        refreshHealth();
      })
      .catch(function () { checkAlarm(); });
  }

  // -------------------------------------------------------------------- wiring
  function wire() {
    $("search").addEventListener("input", function (e) {
      model.filters.search = e.target.value.trim();
      renderTable();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== $("search")) {
        e.preventDefault();
        $("search").focus();
      }
      if (e.key === "Escape") {
        $("inbox").hidden = true;
        $("search").blur();
      }
    });

    Array.prototype.forEach.call($("seg-side").children, function (btn) {
      btn.onclick = function () {
        model.filters.side = btn.dataset.side;
        Array.prototype.forEach.call($("seg-side").children, function (b) { b.classList.toggle("on", b === btn); });
        renderTable();
      };
    });

    $("state-select").onchange = function (e) { model.filters.state = e.target.value; renderTable(); };
    $("sort-select").onchange = function (e) { model.filters.sort = e.target.value; renderTable(); };

    $("btn-reset").onclick = function () {
      model.filters = { search: "", tfs: new Set(), side: "both", grades: new Set(), sessions: new Set(), state: "all", sort: "new" };
      $("search").value = "";
      $("state-select").value = "all";
      $("sort-select").value = "new";
      Array.prototype.forEach.call($("seg-side").children, function (b) { b.classList.toggle("on", b.dataset.side === "both"); });
      buildFacets();
      renderTable();
    };

    $("rows").addEventListener("click", function (e) {
      var tr = e.target.closest("tr[data-key]");
      if (!tr) return;
      var row = model.rows.get(tr.dataset.key);
      if (row) selectRow(row);
    });

    $("btn-refresh-chart").onclick = function () {
      var row = model.selected ? model.rows.get(model.selected) : null;
      if (row) loadChart(row.symbol, row.timeframe);
      else refreshArchive();
    };

    $("btn-inbox").onclick = function () { $("inbox").hidden = !$("inbox").hidden; };
    $("btn-close-inbox").onclick = function () { $("inbox").hidden = true; };
    $("toast-open").onclick = function () { $("inbox").hidden = false; };
    $("btn-read").onclick = function () {
      toastQueue.markRead();
      renderInbox();
      fetch("/api/notifications/read", { method: "POST" }).catch(function () {});
    };

    var quietBtn = $("btn-quiet");
    function paintQuiet() {
      $("quiet-text").textContent = quiet ? "quiet on" : "quiet off";
      quietBtn.title = quiet
        ? "Quiet: at most one toast a minute, one on screen at a time. Everything else waits in the inbox."
        : "Loud: a toast for every actionable event, still one at a time with no duplicates. The inbox is still the record.";
    }
    paintQuiet();
    quietBtn.onclick = function () {
      quiet = !quiet;
      localStorage.setItem("tdigm.quiet", quiet ? "1" : "0");
      toastQueue.setMinGap(quiet ? 60_000 : 8_000);
      renderInbox();
      paintQuiet();
    };

    var resizeTimer = 0;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        var row = model.selected ? model.rows.get(model.selected) : null;
        if (row) loadChart(row.symbol, row.timeframe);
      }, 200);
    });
  }

  // --------------------------------------------------------------------- boot
  function boot() {
    wire();
    renderInbox();
    renderHeader();
    buildFacets();
    renderTable();
    refreshArchive();
    loadNotifications();
    refreshHealth();
    connect();
    poll();
    setInterval(poll, 6000);
    setInterval(refreshHealth, 8000);
    refreshWatch();
    setInterval(refreshWatch, 5000);
    setInterval(refreshArchive, 20000);
    setInterval(loadNotifications, 12000);
    setInterval(checkAlarm, 5000);
    setInterval(function () { if (!model.wsOk) checkAlarm(); }, 3000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
