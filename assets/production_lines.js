/*
 * Production lines (version 2) for the dashboard (/dashboard).
 *
 * Loaded after the dashboard's own script and styled with the classes and
 * colour variables of assets/dashboard.css. It adds:
 *   - a line selector in the header; Line dashboard and Line setup follow it
 *     and each browser remembers its choice;
 *   - the Plant overview, Lines and Products pages;
 *   - Line setup: the line's own settings, then one card per camera (up to
 *     8) holding every setting of that camera: device, job, model and
 *     classes, counting (flow, count lines drawn on its picture, tracking),
 *     code reading, image (resolution, ROI...) and video. One Save;
 *   - on Line dashboard, a camera strip beside the large feed and a list of
 *     recent QR reads;
 *   - Line and Role columns on Cameras, "Used by" on Connections, and a
 *     line filter on Audit log.
 *
 * The dashboard's own code keeps calling the same endpoints. While a line
 * other than Line 1 is selected, the fetch wrapper below adds line_id to the
 * counting, PLC card and send card calls and routes the counting settings
 * to that line, so the existing pages work per line without being rewritten.
 */
(function () {
    "use strict";

    const PRIMARY = "line-1";
    const MAX_CAMERAS_PER_LINE = 8;  // line_config.MAX_CAMERAS_PER_LINE
    const STORAGE_KEY = "selected_line_id";
    const state = {
        lineId: localStorage.getItem(STORAGE_KEY) || PRIMARY,
        lines: [],
        clashes: [],
        detail: null,         // GET /lines/{id} for the selected line
        trigger: null,        // that line's action_trigger, for the settings wrapper
        cameras: [],          // GET /cameras
        models: [],           // GET /models
        endpoints: [],        // GET /system/endpoints (with used_by)
        tileVideo: false,
        auditLine: "",
        productSearch: "",
        productLists: [],     // GET /products/lists (with used_by)
        productListId: localStorage.getItem("selected_product_list") || "",
        maxProductLists: 4,
        timers: {},
        blobUrls: {},
        codeTypes: [],        // GET /qr/code-types (set to the three groups below until it loads)
        secondView: {},       // camera_id -> "live" | "capture" on the Line Dashboard
        captureShown: {},     // where a QR picture is shown -> id of the picture on screen
        bigCamera: {},        // line id -> camera shown large on the Line Dashboard (picked in the strip)
    };

    // ── Small helpers ─────────────────────────────────────────────────────────

    const esc = (value) => (typeof escapeHtml === "function"
        ? escapeHtml(value === null || value === undefined ? "" : String(value))
        : String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
    const level = () => parseInt(localStorage.getItem("clearance_level") || "1", 10);
    const toast = (msg, kind) => { if (typeof showToast === "function") showToast(msg, kind || "info"); };
    const byId = (id) => document.getElementById(id);
    const isTab = (id) => byId(id)?.classList.contains("active");
    const num = (v, digits = 0) => (typeof v === "number" ? v.toFixed(digits) : "0");
    const count = (v) => Number(v || 0).toLocaleString();
    const refreshSelects = () => { if (typeof initCustomSelects === "function") setTimeout(initCustomSelects, 30); };

    async function api(path, options = {}) {
        const opts = { ...options, headers: { ...(options.headers || {}) } };
        if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
            opts.body = JSON.stringify(opts.body);
            opts.headers["Content-Type"] = "application/json";
        }
        const res = await fetch(path, opts);
        let data = null;
        try { data = await res.json(); } catch (_) { /* empty or non-JSON body */ }
        if (!res.ok) {
            const detail = data && data.detail;
            throw new Error(typeof detail === "string" ? detail : (detail ? JSON.stringify(detail) : `HTTP ${res.status}`));
        }
        return data;
    }

    function confirmAction(title, message, onYes) {
        if (typeof showModal === "function") showModal(title, message, true, onYes);
        else if (window.confirm(`${title}\n\n${message}`)) onYes();
    }

    const STATE_LABELS = {
        running: ["Running", "var(--success-color)"],
        stopped: ["Stopped", "var(--text-muted)"],
        idle: ["Idle", "var(--warning-color)"],
        no_camera: ["No camera", "var(--text-muted)"],
        fault: ["Fault", "var(--danger-color)"],
    };
    function statePill(stateName) {
        const [label, color] = STATE_LABELS[stateName] || [stateName || "Unknown", "var(--text-muted)"];
        return `<span class="pl-pill" style="color:${color};border-color:${color}">${esc(label)}</span>`;
    }

    function lineName(id) {
        const line = state.lines.find((l) => l.id === id);
        return line ? line.name : id;
    }

    // ── Styles (layout only; colours come from the dashboard's variables) ─────

    const style = document.createElement("style");
    style.textContent = `
        .pl-line-select { display:flex; align-items:center; gap:8px; margin-left:16px; font-size:12px; color:var(--text-muted); }
        .pl-line-select select { min-width:170px; padding:6px 10px; font-size:13px; }
        .pl-line-select .custom-select-wrap { min-width:170px; }
        .pl-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(290px, 1fr)); gap:16px; }
        .pl-tile { cursor:pointer; margin-bottom:0; }
        .pl-tile:hover { border-color:var(--primary-color); }
        .pl-tile-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:8px; }
        .pl-tile-name { font-size:15px; font-weight:700; color:var(--text-color); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .pl-pill { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.5px; border:1px solid; border-radius:4px; padding:2px 7px; white-space:nowrap; }
        .pl-stats { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px 10px; }
        .pl-stat-label { font-size:10px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.4px; }
        .pl-stat-val { font-size:17px; font-weight:700; color:var(--text-color); font-variant-numeric:tabular-nums; }
        .pl-row { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:10px; font-size:11.5px; color:var(--text-muted); }
        .pl-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:middle; }
        .pl-tile-video { width:100%; aspect-ratio:4/3; object-fit:contain; background:#000; border-radius:6px; margin-bottom:10px; display:block; }
        .pl-tile-video[hidden] { display:none; }
        .pl-table { width:100%; border-collapse:collapse; font-size:12.5px; }
        .pl-table th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.4px; color:var(--text-muted); padding:8px 10px; border-bottom:1px solid var(--border-color); }
        .pl-table td { padding:9px 10px; border-bottom:1px solid var(--border-color); vertical-align:middle; }
        .pl-actions { display:flex; flex-wrap:wrap; gap:6px; }
        .pl-actions .btn-action { padding:4px 10px; font-size:11px; }
        .pl-form-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:14px; }
        .pl-cam-box { border:1px solid var(--border-color); border-radius:8px; padding:14px; }
        .pl-cam-box h4 { margin:0 0 10px; font-size:13px; color:var(--text-color); }
        .pl-note { font-size:11px; color:var(--text-muted); margin-top:4px; }
        .pl-warn { border-left:3px solid var(--warning-color); padding:8px 12px; font-size:12px; margin-top:12px; }
        .pl-classes { max-height:172px; overflow:auto; border:1px solid var(--border-color); border-radius:6px; padding:6px 10px; display:grid; grid-template-columns:repeat(auto-fill, minmax(118px, 1fr)); gap:2px 12px; }
        .pl-class { display:flex; align-items:center; gap:6px; min-width:0; padding:2px 0; font-size:12.5px; color:var(--text-color); cursor:pointer; }
        .pl-class[hidden] { display:none; }
        .pl-class span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .pl-class.unknown span { color:var(--warning-color); }
        .pl-classes-empty { grid-column:1 / -1; font-size:12px; color:var(--text-muted); }
        .pl-class-filter { margin-bottom:6px; padding:5px 9px; font-size:12.5px; }
        .pl-model-state { font-size:11.5px; margin-top:5px; color:var(--text-muted); }
        .pl-model-state .pl-dot { margin-right:5px; }
        .pl-feeds { display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px; }
        .pl-feed-img { width:100%; aspect-ratio:4/3; object-fit:contain; background:#000; border-radius:6px; display:block; }
        .pl-code { font-family:monospace; font-size:12px; }
        .pl-inline { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
        .pl-inline .form-input { width:auto; flex:1; min-width:160px; }
        .pl-feed-row { display:grid; grid-template-columns:minmax(0, 2fr) minmax(0, 1fr); gap:20px; margin-bottom:20px; align-items:start; }
        .pl-feed-row > .card-panel { margin-bottom:0; min-width:0; }
        .pl-strip { display:grid; grid-template-columns:1fr; gap:12px; align-content:start; min-width:0; }
        .pl-strip.many { grid-template-columns:1fr 1fr; }
        .pl-strip-tile { margin-bottom:0; padding:10px 12px; min-width:0; }
        .pl-strip-head { display:flex; align-items:center; gap:6px; min-width:0; }
        .pl-strip-head .pl-dot { margin-right:0; flex:none; }
        .pl-strip-name { font-size:13px; font-weight:700; color:var(--text-color); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; min-width:0; }
        .pl-strip-detail { margin:2px 0 8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .pl-strip-pic { display:block; width:100%; padding:0; border:0; cursor:zoom-in; aspect-ratio:4/3; min-height:0; }
        .pl-strip-pic .video-img { object-fit:contain; }
        .pl-strip-overlay { font-size:11px; font-weight:700; letter-spacing:1.5px; color:rgba(255,255,255,.75); text-transform:uppercase; }
        .pl-strip-figures { margin-top:8px; }
        .pl-strip-tools { margin-top:8px; }
        .pl-strip-tools .pl-seg button { white-space:nowrap; padding:5px 9px; }
        .pl-strip-test { font-size:11px; padding:4px 10px; }
        .pl-badge { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; border-radius:4px; padding:2px 7px; white-space:nowrap; background:var(--panel-head, rgba(127,127,127,.12)); color:var(--text-muted); border:1px solid var(--border-color); flex:none; }
        .pl-badge.job-counting { color:var(--primary-color); border-color:var(--primary-color); }
        .pl-badge.job-station { color:var(--warning-color); border-color:var(--warning-color); }
        .pl-badge.job-qr { color:var(--success-color); border-color:var(--success-color); }
        .pl-cam-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(360px, 1fr)); gap:16px; align-items:start; margin-bottom:20px; }
        .pl-card { margin-bottom:0; min-width:0; padding:14px 16px; }
        .pl-card [hidden] { display:none !important; }
        .pl-card.pl-flash { box-shadow:0 0 0 3px var(--primary-color); transition:box-shadow .3s; }
        .pl-card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; }
        .pl-card-title { display:flex; align-items:center; gap:7px; min-width:0; }
        .pl-card-title .pl-dot { margin-right:0; flex:none; }
        .pl-card-name { font-size:15px; font-weight:700; color:var(--text-color); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; }
        .pl-card-tools { display:flex; gap:4px; flex:none; }
        .pl-icon-btn { display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:6px; border:1px solid var(--border-color); background:transparent; color:var(--text-muted); cursor:pointer; }
        .pl-icon-btn:hover:not(:disabled) { color:var(--text-color); border-color:var(--primary-color); }
        .pl-icon-btn.danger:hover:not(:disabled) { color:var(--danger-color); border-color:var(--danger-color); }
        .pl-icon-btn:disabled { opacity:.4; cursor:not-allowed; }
        .pl-card-sub { font-size:11.5px; color:var(--text-muted); margin:2px 0 10px; }
        .pl-card-live { position:relative; aspect-ratio:16/9; background:var(--video-bg, #000); border-radius:6px; overflow:hidden; margin-bottom:10px; }
        .pl-card-live img { position:absolute; inset:0; width:100%; height:100%; object-fit:contain; opacity:0; }
        .pl-card-live img.on { opacity:1; }
        .pl-card-live-note { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:11px; letter-spacing:1px; text-transform:uppercase; color:rgba(255,255,255,.6); pointer-events:none; }
        .pl-fold { border-top:1px solid var(--border-color); }
        .pl-fold-head { display:flex; width:100%; align-items:center; justify-content:space-between; gap:8px; padding:10px 2px; background:transparent; border:0; color:var(--text-color); font-size:13px; font-weight:600; cursor:pointer; text-align:left; }
        .pl-fold-head svg { transition:transform .15s; color:var(--text-muted); flex:none; }
        .pl-fold.open .pl-fold-head svg { transform:rotate(180deg); }
        .pl-fold-body { display:none; padding:2px 2px 14px; }
        .pl-fold.open .pl-fold-body { display:block; }
        .pl-form-tight { grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:10px 12px; }
        .pl-form-tight .form-group { margin-bottom:8px; }
        .pl-check { font-size:12.5px; color:var(--text-color); cursor:pointer; align-items:flex-start; flex-wrap:nowrap; }
        .pl-check input { margin-top:2px; flex:none; }
        .pl-lines-pic { position:relative; width:100%; aspect-ratio:4/3; background:var(--video-bg, #111); border:1px solid var(--border-color); border-radius:6px; overflow:hidden; margin:2px 0 10px; cursor:crosshair; touch-action:none; user-select:none; }
        .pl-lines-pic img { position:absolute; inset:0; width:100%; height:100%; object-fit:fill; pointer-events:none; }
        .pl-lines-pic canvas { position:absolute; inset:0; width:100%; height:100%; }
        .pl-lines-note { position:absolute; left:8px; bottom:6px; font-size:11px; color:rgba(255,255,255,.75); background:rgba(0,0,0,.45); padding:2px 6px; border-radius:4px; }
        .pl-link { background:none; border:0; padding:0; color:#9cc3ff; font-size:11px; text-decoration:underline; cursor:pointer; }
        .pl-advanced summary { cursor:pointer; font-size:12.5px; font-weight:600; color:var(--text-color); padding:4px 0; }
        .pl-add-card { border-style:dashed; }
        .pl-setup-actions { display:flex; flex-wrap:wrap; gap:8px 10px; align-items:center; }
        .pl-unsaved { font-size:11px; font-weight:700; color:var(--warning-color); margin-left:8px; }
        .dashboard-grid.pl-grid-single { grid-template-columns:1fr; }
        .pl-inline-capture { position:relative; margin-bottom:12px; max-width:640px; }
        .pl-inline-capture[hidden] { display:none; }
        .pl-feed-caption { position:absolute; left:0; right:0; bottom:0; z-index:5; display:flex; flex-wrap:wrap; gap:4px 14px; padding:8px 12px; font-size:12px; color:#fff; background:rgba(0,0,0,.62); }
        .pl-seg { display:inline-flex; border:1px solid var(--border-color); border-radius:6px; overflow:hidden; }
        .pl-seg button { background:transparent; border:0; padding:5px 11px; font-size:11px; font-weight:600; color:var(--text-muted); cursor:pointer; }
        .pl-seg button.active { background:var(--primary-color); color:#fff; }
        @media (max-width:992px) { .pl-feed-row { grid-template-columns:1fr; } .pl-strip, .pl-strip.many { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
        @media (max-width:520px) { .pl-cam-grid { grid-template-columns:1fr; } .pl-card { padding:12px; } }
        @media (max-width:700px) { .pl-line-select { margin-left:8px; } .pl-line-select > span { display:none; } .pl-stats { grid-template-columns:repeat(2, 1fr); } }
    `;
    document.head.appendChild(style);

    // ── Per-line scoping of the existing dashboard requests ───────────────────

    // Browser-side caches the dashboards keep; each line gets its own copy.
    const PER_LINE_STORAGE = new Set(["plc_action_trigger_cards"]);
    const storageKey = (key) => (PER_LINE_STORAGE.has(key) && state.lineId !== PRIMARY ? `${key}::${state.lineId}` : key);
    const nativeGet = Storage.prototype.getItem;
    const nativeSet = Storage.prototype.setItem;
    const nativeRemove = Storage.prototype.removeItem;
    Storage.prototype.getItem = function (key) { return nativeGet.call(this, this === window.localStorage ? storageKey(key) : key); };
    Storage.prototype.setItem = function (key, value) { return nativeSet.call(this, this === window.localStorage ? storageKey(key) : key, value); };
    Storage.prototype.removeItem = function (key) { return nativeRemove.call(this, this === window.localStorage ? storageKey(key) : key); };

    const LINE_SCOPED_PATHS = ["/api/v1/counting/", "/api/v1/plc/actions", "/api/v1/send/actions"];
    const baseFetch = window.fetch;

    async function lineTrigger(force) {
        if (!force && state.trigger) return state.trigger;
        const detail = await (await baseFetch(`/api/v1/lines/${encodeURIComponent(state.lineId)}`)).json();
        state.trigger = detail.action_trigger || {};
        return state.trigger;
    }

    async function withLineTrigger(res, isPoll) {
        if (!res.ok) return res;
        const data = await res.clone().json();
        const holder = isPoll ? data.settings : data;
        if (!holder) return res;
        try {
            holder.action_trigger = await lineTrigger(true);
        } catch (_) {
            return res;
        }
        return new Response(JSON.stringify(data), { status: res.status, headers: res.headers });
    }

    async function saveLineTrigger(body) {
        const current = await lineTrigger(true);
        const update = { ...body.action_trigger };
        const payload = { action_trigger: { ...current, ...update } };
        if (Array.isArray(update.plc_actions)) {
            payload.plc_actions = update.plc_actions;
            delete payload.action_trigger.plc_actions;
        }
        const saved = await baseFetch(`/api/v1/lines/${encodeURIComponent(state.lineId)}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!saved.ok) return saved;
        state.trigger = (await saved.clone().json()).action_trigger || payload.action_trigger;
        const rest = { ...body };
        delete rest.action_trigger;
        if (Object.keys(rest).length) {
            const other = await baseFetch("/api/v1/system/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(rest),
            });
            if (!other.ok) return other;
        }
        return withLineTrigger(await baseFetch("/api/v1/system/settings"), false);
    }

    window.fetch = async (input, options = {}) => {
        if (typeof input !== "string" || state.lineId === PRIMARY) return baseFetch(input, options);
        let url = input;
        const method = String(options.method || "GET").toUpperCase();
        if (LINE_SCOPED_PATHS.some((p) => url.startsWith(p)) && !/[?&]line_id=/.test(url)) {
            url += (url.includes("?") ? "&" : "?") + "line_id=" + encodeURIComponent(state.lineId);
        }
        if (url.startsWith("/api/v1/system/settings")) {
            if (method === "GET") return withLineTrigger(await baseFetch(url, options), false);
            if (typeof options.body === "string") {
                let body = null;
                try { body = JSON.parse(options.body); } catch (_) { /* not JSON */ }
                if (body && body.action_trigger) return saveLineTrigger(body);
            }
        }
        if (url.startsWith("/api/v1/system/poll-changes")) return withLineTrigger(await baseFetch(url, options), true);
        return baseFetch(url, options);
    };

    // ── Data ──────────────────────────────────────────────────────────────────

    async function loadLines() {
        const data = await api("/api/v1/lines");
        state.lines = data.lines || [];
        state.clashes = data.plc_address_clashes || [];
        // The line this browser remembers may be gone (deleted from another
        // browser, or the server was set up again). Everything asked for it so
        // far was refused, so show Line 1 and load it again.
        const gone = !state.lines.some((l) => l.id === state.lineId);
        if (gone) setLine(PRIMARY);
        renderSelector();
        if (gone) reloadForLine();
        return state.lines;
    }

    // The dashboard's own pages reload through the fetch wrapper, for the selected line.
    function reloadForLine() {
        if (typeof loadServerSettings === "function") loadServerSettings();
        if (typeof loadPlcActionCards === "function") loadPlcActionCards();
        if (typeof loadSendActionCards === "function") loadSendActionCards();
        if (typeof fetchStats === "function") fetchStats();
        if (typeof checkActiveStream === "function") checkActiveStream();
    }

    async function loadDetail() {
        state.detail = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}`);
        state.trigger = state.detail.action_trigger || {};
        // Lets a dashboard show the line's state without fetching it again.
        window.dispatchEvent(new CustomEvent("pl:detail", { detail: state.detail }));
        return state.detail;
    }

    async function loadCamerasList() {
        try { state.cameras = await api("/api/v1/cameras"); } catch (_) { state.cameras = []; }
        return state.cameras;
    }

    async function loadModelsList() {
        try { state.models = await api("/api/v1/models"); } catch (_) { state.models = []; }
        return state.models;
    }

    // What a camera that reads codes can be limited to: all types, the 2D or the 1D
    // ones, or one of the types the server's reader supports. Asked for once.
    const CODE_TYPE_GROUPS = [
        { value: "all", label: "1D and 2D codes (all types)", kind: "group" },
        { value: "2d", label: "2D codes (all 2D types)", kind: "group" },
        { value: "1d", label: "1D barcodes (all 1D types)", kind: "group" },
    ];
    state.codeTypes = CODE_TYPE_GROUPS;
    async function loadCodeTypes() {
        if (state.codeTypes.length > CODE_TYPE_GROUPS.length) return state.codeTypes;
        try {
            const data = await api("/api/v1/qr/code-types");
            if (data && Array.isArray(data.types) && data.types.length) state.codeTypes = data.types;
        } catch (_) { /* the three groups still work */ }
        return state.codeTypes;
    }

    function codeTypeLabel(value) {
        const type = state.codeTypes.find((t) => t.value === (value || "all"));
        if (!type) return value || "";
        return type.kind === "group" ? type.label : `${type.label} only`;
    }

    function cameraName(id) {
        // The dashboard's own camera list is loaded at sign-in; this script's
        // copy only once a page that needs it opens.
        const shared = typeof _allCamsData !== "undefined" && Array.isArray(_allCamsData) ? _allCamsData : [];
        const cam = state.cameras.find((c) => c.id === id) || shared.find((c) => c.id === id);
        return cam ? cam.name : id;
    }

    // ── Line selector ─────────────────────────────────────────────────────────

    function renderSelector() {
        const title = byId("pageTitle");
        if (!title) return;
        let wrap = byId("plLineSelectWrap");
        if (!wrap) {
            // A <div>, not a <label>: a label passes every click on the
            // dropdown on to the hidden <select>, and that second click
            // closes the dropdown before a line can be picked.
            wrap = document.createElement("div");
            wrap.id = "plLineSelectWrap";
            wrap.className = "pl-line-select";
            wrap.innerHTML = `<span>Line:</span><select id="plLineSelect" class="form-input" aria-label="Production line"></select>`;
            title.insertAdjacentElement("afterend", wrap);
            byId("plLineSelect").addEventListener("change", (e) => switchLine(e.target.value));
        }
        const select = byId("plLineSelect");
        select.innerHTML = state.lines.map((l) => `<option value="${esc(l.id)}" ${l.id === state.lineId ? "selected" : ""}>${esc(l.name)}</option>`).join("");
        if (typeof select._refreshCustomSelect === "function") select._refreshCustomSelect();
        else refreshSelects();
    }

    function setLine(id, persist = true) {
        state.lineId = id || PRIMARY;
        state.trigger = null;
        state.detail = null;
        if (persist) localStorage.setItem(STORAGE_KEY, state.lineId);
    }

    function switchLine(id) {
        if (id === state.lineId) return;
        const go = async () => {
            setLine(id);
            renderSelector();
            try { await loadDetail(); } catch (_) { /* shown by the pages below */ }
            reloadForLine();
            if (isTab("tabActionTrigger")) renderLineSetup();
            if (isTab("tabDashboard")) renderLineExtras();
            toast(`Showing ${lineName(state.lineId)}`, "info");
        };
        let pending = false;
        try { pending = Boolean(_plcActionPendingChanges) || Boolean(_sendActionPendingChanges); } catch (_) { pending = false; }
        if (pending || setup.dirty) {
            const what = setup.dirty ? "The line setup (cameras and their settings)" : "The PLC action or send cards";
            confirmAction("Unsaved changes", `${what} of this line ${setup.dirty ? "has" : "have"} changes that are not saved. Switch lines and discard them?`, () => { setup.dirty = false; go(); });
            const select = byId("plLineSelect");
            if (select) { select.value = state.lineId; if (select._refreshCustomSelect) select._refreshCustomSelect(); }
        } else {
            go();
        }
    }

    // ── Plant Overview ────────────────────────────────────────────────────────

    function ensurePage(id, title, bodyHtml) {
        let page = byId(id);
        if (page) return page;
        page = document.createElement("div");
        page.id = id;
        page.className = "tab-page";
        page.innerHTML = bodyHtml;
        const anchor = byId("tabDashboard");
        anchor.parentNode.insertBefore(page, anchor);
        TAB_TITLES[id] = title;
        return page;
    }

    // A QR reader, or a vision camera with Read codes on.
    const readsCodes = (c) => Boolean(c && (c.role === "qr" || c.read_codes));
    const roleLabel = (c, short) => c.role === "qr"
        ? (short ? "QR" : "QR / barcode reader")
        : (c.read_codes ? (short ? "Vision + QR" : "Vision + QR / barcode") : "Vision");

    // The tile shows the line's counting camera, like the Line Dashboard.
    function tileVideoCamera(row) {
        if (!state.tileVideo) return null;
        return row.cameras.find((c) => c.counting && c.connected) || row.cameras.find((c) => c.role === "vision" && c.connected) || row.cameras.find((c) => c.connected) || null;
    }

    const tileHeadHtml = (row) => `<span class="pl-tile-name">${esc(row.name)}</span>${statePill(row.state)}`;

    function tileBodyHtml(row) {
        const cams = row.cameras.length
            ? row.cameras.map((c) => `<span><span class="pl-dot" style="background:${c.connected ? "var(--success-color)" : "var(--danger-color)"}"></span>${esc(c.name || cameraName(c.camera_id))} · ${roleLabel(c, true)}</span>`).join("")
            : `<span>No camera assigned</span>`;
        const qr = row.has_qr
            ? `<div class="pl-row"><span>Codes read <b>${row.qr.codes_read}</b></span><span>Unknown <b style="color:var(--danger-color)">${row.qr.unknown}</b></span><span>No read <b style="color:var(--warning-color)">${row.qr.no_reads}</b></span></div>`
            : "";
        return `
                <div class="pl-stats">
                    <div><div class="pl-stat-label">Total</div><div class="pl-stat-val">${count(row.total_inspected)}</div></div>
                    <div><div class="pl-stat-label">Good</div><div class="pl-stat-val" style="color:var(--success-color)">${count(row.good_count)}</div></div>
                    <div><div class="pl-stat-label">Rejected</div><div class="pl-stat-val" style="color:var(--danger-color)">${count(row.rejected_count)}</div></div>
                    <div><div class="pl-stat-label">Per min</div><div class="pl-stat-val">${num(row.products_per_minute, 1)}</div></div>
                    <div><div class="pl-stat-label">Yield</div><div class="pl-stat-val">${num(row.yield_percentage, 1)}%</div></div>
                    <div><div class="pl-stat-label">Frames/s</div><div class="pl-stat-val" style="${row.min_fps && row.state === "running" && row.processed_fps < row.min_fps ? "color:var(--danger-color)" : ""}">${num(row.processed_fps, 1)}</div></div>
                </div>
                <div class="pl-row">${cams}</div>
                ${qr}
                <div class="pl-row"><span>Active alarms <b style="${row.active_alarms ? "color:var(--danger-color)" : ""}">${row.active_alarms}</b></span><span>Model: ${esc(row.model_name || "—")}</span></div>`;
    }

    // Writes a part of a tile only when it changed, so the readings update in
    // place and the tile's video is never rebuilt with them.
    function setPart(el, html) {
        if (el.dataset.html === html) return;
        el.dataset.html = html;
        el.innerHTML = html;
    }

    function newTile(lineId) {
        const tile = document.createElement("div");
        tile.className = "card-panel pl-tile";
        tile.dataset.line = lineId;
        tile.innerHTML = `<div class="pl-tile-head"></div><img class="pl-tile-video" alt="" hidden><div class="pl-tile-body"></div>`;
        tile.addEventListener("click", () => {
            if (tile.dataset.line !== state.lineId) switchLine(tile.dataset.line);
            switchTab("tabDashboard");
        });
        return tile;
    }

    async function refreshOverview() {
        const grid = byId("plOverviewGrid");
        // Nothing is fetched while the page is not on screen.
        if (!grid || !isTab("tabOverview") || document.hidden) return;
        let rows;
        try {
            rows = (await api("/api/v1/lines/overview")).lines || [];
        } catch (e) {
            stopTileFeeds();
            grid.innerHTML = `<div class="card-panel">Could not load the lines: ${esc(e.message)}</div>`;
            return;
        }
        if (!isTab("tabOverview") || document.hidden) return;  // left while the request was out
        const tiles = new Map([...grid.querySelectorAll(".pl-tile")].map((t) => [t.dataset.line, t]));
        if (grid.children.length !== tiles.size) grid.innerHTML = "";  // a message was shown
        if (!rows.length) {
            stopTileFeeds();
            grid.innerHTML = `<div class="card-panel">No production lines.</div>`;
            return;
        }
        rows.forEach((row, index) => {
            const tile = tiles.get(row.id) || newTile(row.id);
            tiles.delete(row.id);
            if (grid.children[index] !== tile) grid.insertBefore(tile, grid.children[index] || null);
            tile.title = `Open ${row.name}`;
            setPart(tile.querySelector(".pl-tile-head"), tileHeadHtml(row));
            setPart(tile.querySelector(".pl-tile-body"), tileBodyHtml(row));
            const img = tile.querySelector(".pl-tile-video");
            const cam = tileVideoCamera(row);
            img.hidden = !cam;
            if (cam) {
                img.alt = `Live video of ${row.name}`;
                startTileFeed(row.id, cam.camera_id, img);
            } else {
                stopTileFeed(row.id);
            }
        });
        tiles.forEach((tile, lineId) => { stopTileFeed(lineId); tile.remove(); });
    }

    // ── Live video on the tiles ───────────────────────────────────────────────

    // Each tile asks for its next picture when the previous one has arrived, so
    // requests never pile up. A tile shows at most TILE_MAX_FPS pictures a
    // second and all tiles together about PAGE_FPS_BUDGET, which leaves the
    // server's per-client request limit to the readings and the other pages.
    const TILE_MAX_FPS = 8;
    const PAGE_FPS_BUDGET = 25;
    const TILE_WIDTH = 480;
    const TILE_RETRY_MS = 1000;
    const tileFeeds = new Map();  // line id -> { camera, img, controller, url, frames }
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const overviewShown = () => isTab("tabOverview") && !document.hidden;
    const tileInterval = () => 1000 / Math.min(TILE_MAX_FPS, PAGE_FPS_BUDGET / Math.max(1, tileFeeds.size));

    function startTileFeed(lineId, cameraId, img) {
        const running = tileFeeds.get(lineId);
        if (running && running.camera === cameraId && running.img === img) return;
        stopTileFeed(lineId);
        const feed = { camera: cameraId, img, controller: new AbortController(), url: null, frames: 0 };
        tileFeeds.set(lineId, feed);
        runTileFeed(lineId, feed);
    }

    function stopTileFeed(lineId) {
        const feed = tileFeeds.get(lineId);
        if (!feed) return;
        tileFeeds.delete(lineId);
        feed.controller.abort();
        if (feed.url) URL.revokeObjectURL(feed.url);
    }

    function stopTileFeeds() {
        [...tileFeeds.keys()].forEach(stopTileFeed);
    }

    async function runTileFeed(lineId, feed) {
        const live = () => tileFeeds.get(lineId) === feed;
        while (live()) {
            if (!overviewShown() || !state.tileVideo) { stopTileFeed(lineId); return; }
            const asked = performance.now();
            let wait = TILE_RETRY_MS;  // camera offline or the server busy: try again later
            try {
                const res = await fetch(`/api/v1/vision/annotated/camera/${encodeURIComponent(feed.camera)}?max_width=${TILE_WIDTH}`, { cache: "no-store", signal: feed.controller.signal });
                if (res.ok) {
                    const blob = await res.blob();
                    if (!live()) return;
                    const url = URL.createObjectURL(blob);
                    const previous = feed.url;
                    feed.url = url;
                    feed.img.src = url;
                    try { await feed.img.decode(); } catch (_) { /* replaced or not a picture */ }
                    if (previous) URL.revokeObjectURL(previous);
                    feed.frames += 1;
                    wait = Math.max(0, tileInterval() - (performance.now() - asked));
                }
            } catch (e) {
                if (e.name === "AbortError") return;
            }
            await sleep(wait);
        }
    }

    function buildOverviewPage() {
        ensurePage("tabOverview", "Plant overview", `
            <div class="card-panel">
                <div class="card-panel-header">
                    <span class="card-panel-title">All production lines</span>
                    <label class="pl-inline" style="font-size:12px;color:var(--text-muted);cursor:pointer">
                        <input type="checkbox" id="plTileVideo"> Live video on tiles
                    </label>
                </div>
                <div class="pl-note" style="margin-top:0">Click a line to open its dashboard. Live video is off by default to save network bandwidth; the tiles show a small picture, the full one is on the Line dashboard.</div>
            </div>
            <div class="pl-grid" id="plOverviewGrid"></div>`);
        byId("plTileVideo").addEventListener("change", (e) => {
            state.tileVideo = e.target.checked;
            if (!state.tileVideo) stopTileFeeds();
            refreshOverview();
        });
    }

    // ── Lines page ────────────────────────────────────────────────────────────

    function buildLinesPage() {
        ensurePage("tabLines", "Lines", `
            <div class="card-panel">
                <div class="card-panel-header">
                    <span class="card-panel-title">Production lines</span>
                    <div class="pl-inline">
                        <input type="text" id="plNewLineName" class="form-input" maxlength="64" placeholder="New line name, e.g. Packing 2">
                        <button class="btn-action btn-blue" id="plAddLine">Add line</button>
                    </div>
                </div>
                <div class="table-responsive"><table class="pl-table">
                    <thead><tr><th>Line</th><th>State</th><th>Cameras</th><th>Model</th><th>Sync</th><th>Total</th><th>Actions</th></tr></thead>
                    <tbody id="plLinesBody"><tr><td colspan="7">Loading…</td></tr></tbody>
                </table></div>
                <div id="plClashes"></div>
                <div class="pl-note">A new line starts stopped and without cameras. Open its setup to assign cameras, then start it. Line 1 cannot be deleted.</div>
            </div>`);
        byId("plAddLine").addEventListener("click", addLine);
        byId("plNewLineName").addEventListener("keydown", (e) => { if (e.key === "Enter") addLine(); });
    }

    async function refreshLinesPage() {
        const body = byId("plLinesBody");
        if (!body) return;
        try {
            await Promise.all([loadLines(), loadCamerasList()]);
        } catch (e) {
            body.innerHTML = `<tr><td colspan="7">Could not load the lines: ${esc(e.message)}</td></tr>`;
            return;
        }
        body.innerHTML = state.lines.map((l) => {
            const st = l.status || {};
            const cams = (l.cameras || []).map((c) => `${esc(cameraName(c.camera_id))} (${roleLabel(c, true)}${c.counting ? ", counting" : ""})`).join("<br>") || "—";
            const running = l.enabled;
            // Each vision camera runs its own model: say so when one has none, or its model is not in memory.
            const vision = (st.cameras || []).filter((c) => c.role === "vision");
            const modelNote = vision.some((c) => !c.model_id) ? "A camera has no model"
                : vision.some((c) => !c.model_loaded) ? "Not loaded" : "";
            return `<tr>
                <td><b>${esc(l.name)}</b><div class="pl-note pl-code">${esc(l.id)}</div></td>
                <td>${statePill(st.state)}</td>
                <td style="font-size:12px">${cams}</td>
                <td>${esc(st.model_name || "—")}${modelNote ? `<div class="pl-note" style="color:var(--danger-color)">${modelNote}</div>` : ""}</td>
                <td>${l.sync && l.sync.enabled ? `On (${l.sync.window_ms} ms)` : "Off"}</td>
                <td>${count(st.total_inspected)}</td>
                <td><div class="pl-actions">
                    <button class="btn-action btn-blue" data-act="logic" data-id="${esc(l.id)}">Setup</button>
                    <button class="btn-action ${running ? "btn-outline" : "btn-blue"}" data-act="${running ? "stop" : "start"}" data-id="${esc(l.id)}">${running ? "Stop" : "Start"}</button>
                    <button class="btn-action btn-outline" data-act="rename" data-id="${esc(l.id)}">Rename</button>
                    <button class="btn-action btn-outline" data-act="clone" data-id="${esc(l.id)}">Clone</button>
                    ${l.id === PRIMARY ? "" : `<button class="btn-action btn-red" data-act="delete" data-id="${esc(l.id)}">Delete</button>`}
                </div></td>
            </tr>`;
        }).join("");
        body.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", () => lineAction(b.dataset.act, b.dataset.id)));
        const clashes = byId("plClashes");
        clashes.innerHTML = state.clashes.length
            ? `<div class="pl-warn"><b>PLC address used by more than one line</b> (allowed, but check it is intended, e.g. a shared tower light):<br>${state.clashes.map((c) =>
                `${esc(c.address)} on channel ${esc(channelName(c.plc_endpoint_id))}: ${c.lines.map((id) => `${esc(lineName(id))} (${(c.cards[id] || []).map(esc).join(", ")})`).join("; ")}`).join("<br>")}</div>`
            : "";
    }

    function channelName(id) {
        const all = (typeof allCommsEndpoints !== "undefined" ? allCommsEndpoints : []) || [];
        const ep = all.find((e) => e.id === id);
        return ep ? ep.name : id;
    }

    async function addLine() {
        const input = byId("plNewLineName");
        const name = (input.value || "").trim();
        if (!name) { toast("Enter a name for the new line", "warning"); return; }
        try {
            const line = await api("/api/v1/lines", { method: "POST", body: { name } });
            input.value = "";
            toast(`Line '${line.name}' created`, "success");
            await refreshLinesPage();
        } catch (e) { toast(`Could not create the line: ${e.message}`, "warning"); }
    }

    async function lineAction(act, id) {
        const name = lineName(id);
        try {
            if (act === "logic") {
                if (id !== state.lineId) switchLine(id);
                switchTab("tabActionTrigger");
                return;
            }
            if (act === "start" || act === "stop") {
                const res = await api(`/api/v1/lines/${encodeURIComponent(id)}/${act}`, { method: "POST" });
                const errors = Object.entries(res.camera_errors || {});
                if (errors.length) toast(`${name} started, but ${errors.length} camera(s) did not connect: ${errors.map(([c, e]) => `${cameraName(c)}: ${e}`).join("; ")}`, "warning");
                else toast(`${name} ${act === "start" ? "started" : "stopped"}`, "success");
            } else if (act === "rename") {
                const newName = window.prompt(`New name for ${name}:`, name);
                if (!newName || newName.trim() === name) return;
                await api(`/api/v1/lines/${encodeURIComponent(id)}`, { method: "PUT", body: { name: newName.trim() } });
                toast("Line renamed", "success");
            } else if (act === "clone") {
                const newName = window.prompt(`Name for the copy of ${name} (cameras are not copied):`, `${name} copy`);
                if (!newName) return;
                await api(`/api/v1/lines/${encodeURIComponent(id)}/clone`, { method: "POST", body: { name: newName.trim() } });
                toast("Line copied", "success");
            } else if (act === "delete") {
                confirmAction("Delete production line", `Delete ${name}? Its logic and PLC action cards are removed; its cameras stay connected but unassigned.`, async () => {
                    try {
                        await api(`/api/v1/lines/${encodeURIComponent(id)}`, { method: "DELETE" });
                        if (id === state.lineId) switchLine(PRIMARY);
                        toast("Line deleted", "success");
                        refreshLinesPage();
                    } catch (e) { toast(`Could not delete the line: ${e.message}`, "warning"); }
                });
                return;
            }
            refreshLinesPage();
        } catch (e) { toast(`${name}: ${e.message}`, "warning"); }
    }

    // ── Line setup: the line, then one card per camera ────────────────────────

    // A camera's job on the line. "Code reader" is a QR / barcode reader; a
    // vision job with "+ code reader" also reads codes (read_codes).
    const JOBS = {
        counting: { label: "Counting", short: "Counting", role: "vision", counting: true, codes: false },
        station: { label: "Inspection station", short: "Station", role: "vision", counting: false, codes: false },
        qr: { label: "Code reader", short: "Code reader", role: "qr", counting: false, codes: true },
        counting_qr: { label: "Counting + code reader", short: "Counting + codes", role: "vision", counting: true, codes: true },
        station_qr: { label: "Inspection station + code reader", short: "Station + codes", role: "vision", counting: false, codes: true },
    };
    const START_JOBS = ["counting", "station", "qr", "counting_qr"];
    const jobOf = (cam) => (cam.role === "qr" ? "qr" : (cam.counting ? "counting" : "station") + (cam.read_codes ? "_qr" : ""));
    const isVisionJob = (job) => JOBS[job] && JOBS[job].role === "vision";
    const isCountingJob = (job) => Boolean(JOBS[job] && JOBS[job].counting);

    // The flow direction as one choice, saved as orientation + direction with lines A < B.
    const FLOWS = {
        down: { label: "Top → bottom", orientation: "horizontal", direction: "forward" },
        up: { label: "Bottom → top", orientation: "horizontal", direction: "backward" },
        right: { label: "Left → right", orientation: "vertical", direction: "forward" },
        left: { label: "Right → left", orientation: "vertical", direction: "backward" },
    };
    const flowOf = (orientation, direction) => (orientation === "vertical"
        ? (direction === "backward" ? "left" : "right")
        : (direction === "backward" ? "up" : "down"));

    // Tracking settings a camera may change (line_config.TRACKING_LIMITS) and their
    // defaults (CountingConfig): [label, min, max, step, default].
    const TRACKING = {
        min_hits: ["Min hits (frames to confirm a product)", 1, 10, 1, 2],
        max_missed_frames: ["Missed frames before a product is dropped", 1, 120, 1, 15],
        max_speed_pixels: ["Max speed (px per frame)", 10, 1000, 1, 120],
        match_threshold: ["Match threshold", 0.1, 2, 0.05, 0.7],
        position_tolerance: ["Position tolerance (px)", 20, 600, 1, 180],
        track_high_thresh: ["High confidence", 0.05, 1, 0.05, 0.5],
        track_low_thresh: ["Low confidence", 0.01, 0.9, 0.01, 0.15],
    };
    const DEFAULT_LINES = { line1_position: 0.35, line2_position: 0.65, orientation: "horizontal", direction: "forward" };

    const FOLDS = [
        ["camera", "Camera"],
        ["job", "Job"],
        ["model", "AI model and classes"],
        ["counting", "Counting"],
        ["codes", "Codes"],
        ["image", "Image"],
        ["video", "Video"],
    ];
    // Which folds a job shows.
    const foldShown = (fold, job) => (fold === "model" || fold === "counting" ? isVisionJob(job) : fold === "codes" ? JOBS[job].codes : true);

    const setup = {
        lineId: null,
        dirty: false,
        cards: new Map(),     // card key -> { picks, cam (camera row), initialSettings, initialName, feed }
        nextKey: 1,
        focusCamera: null,    // open this camera's card once Line setup is drawn (Cameras page "Settings")
    };

    const cardsOnPage = () => [...document.querySelectorAll("#plCamGrid .pl-card:not(.pl-add-card)")];
    const cardField = (card, name) => card.querySelector(`[data-f="${name}"]`);
    const cardValue = (card, name) => (cardField(card, name) ? cardField(card, name).value : "");
    const cardJob = (card) => cardValue(card, "job") || "counting";
    const cameraRow = (id) => state.cameras.find((c) => c.id === id) || null;

    function markDirty() {
        if (setup.dirty) return;
        setup.dirty = true;
        const note = byId("plSetupDirty");
        if (note) note.hidden = false;
    }

    // Which line other than this one owns each camera.
    function cameraOwners() {
        const owners = {};
        state.lines.forEach((l) => {
            if (l.id === state.lineId) return;
            (l.cameras || []).forEach((c) => { owners[c.camera_id] = l.name; });
        });
        return owners;
    }

    function cameraConnected(id) {
        const live = ((state.detail && state.detail.status && state.detail.status.cameras) || []).find((c) => c.camera_id === id);
        if (live && live.connected) return true;
        const row = cameraRow(id);
        return Boolean(row && (row.connection_state === "connected" || (row.is_active && !row.connection_state)));
    }

    // The options of a camera select: cameras on another line or on another card are greyed out.
    function deviceOptions(selected, card) {
        const owners = cameraOwners();
        const onCards = new Set(cardsOnPage().filter((c) => c !== card).map((c) => c.dataset.camera));
        const options = state.cameras.map((c) => {
            const taken = owners[c.id] ? `on ${owners[c.id]}` : onCards.has(c.id) ? "on another card" : "";
            return `<option value="${esc(c.id)}" ${c.id === selected ? "selected" : ""} ${taken && c.id !== selected ? "disabled" : ""}>${esc(c.name)} (${esc(String(c.type || "").toUpperCase())})${taken ? ` · ${esc(taken)}` : ""}</option>`;
        });
        if (selected && !cameraRow(selected)) options.unshift(`<option value="${esc(selected)}" selected>${esc(selected)} (not found)</option>`);
        return options.join("");
    }

    function modelOptions(modelId) {
        const models = !modelId || state.models.some((m) => m.id === modelId)
            ? state.models
            : state.models.concat([{ id: modelId, name: "(deleted model)", version: "", classes: [] }]);
        return [`<option value="" ${modelId ? "" : "selected"}>— Choose a model —</option>`].concat(models.map((m) =>
            `<option value="${esc(m.id)}" ${m.id === modelId ? "selected" : ""}>${esc(m.name)} ${esc(m.version || "")}</option>`)).join("");
    }

    function codeTypeOptions(codeType) {
        const types = state.codeTypes.some((t) => t.value === codeType)
            ? state.codeTypes
            : state.codeTypes.concat([{ value: codeType, label: codeType, kind: "saved" }]);
        const KIND_PREFIX = { "2d": "2D · ", "1d": "1D · " };
        return types.map((t) => `<option value="${esc(t.value)}" ${t.value === codeType ? "selected" : ""}>${esc((KIND_PREFIX[t.kind] || "") + t.label)}</option>`).join("");
    }

    function listOptions(listId) {
        const lists = state.productLists.some((l) => l.id === listId) || !listId
            ? state.productLists
            : state.productLists.concat([{ id: listId, name: "(deleted list)", count: 0 }]);
        return [`<option value="" ${listId ? "" : "selected"}>— None —</option>`].concat(lists.map((l) =>
            `<option value="${esc(l.id)}" ${l.id === listId ? "selected" : ""}>${esc(l.name)} (${count(l.count)} code${l.count === 1 ? "" : "s"})</option>`)).join("");
    }

    const option = (value, label, current) => `<option value="${value}" ${current === value ? "selected" : ""}>${label}</option>`;
    const numOrEmpty = (v) => (v === null || v === undefined || v === "" ? "" : v);

    // What the folds remember being open, per camera, in this browser.
    function openFolds(cameraId, fallback) {
        try {
            const saved = JSON.parse(localStorage.getItem(`pl_card_folds::${cameraId}`) || "null");
            if (Array.isArray(saved)) return new Set(saved);
        } catch (_) { /* not saved */ }
        return new Set(fallback);
    }
    function rememberFolds(card) {
        const open = [...card.querySelectorAll(".pl-fold.open")].map((f) => f.dataset.fold);
        try { localStorage.setItem(`pl_card_folds::${card.dataset.camera}`, JSON.stringify(open)); } catch (_) { /* storage off */ }
    }

    function foldHtml(name, title, body, open) {
        return `<section class="pl-fold${open ? " open" : ""}" data-fold="${name}">
            <button type="button" class="pl-fold-head" aria-expanded="${open ? "true" : "false"}"><span>${title}</span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg></button>
            <div class="pl-fold-body">${body}</div></section>`;
    }

    // One camera card. ``cam`` is the line's camera entry (or a new one), ``trigger`` the line's action_trigger.
    function cameraCardHtml(key, cam, trigger, openSet) {
        const job = jobOf(cam);
        const row = cameraRow(cam.camera_id) || { name: cam.camera_id, type: "" };
        const eff = (k) => (cam[k] !== undefined && cam[k] !== null && cam[k] !== "" ? cam[k] : (trigger[k] !== undefined && trigger[k] !== null && trigger[k] !== "" ? trigger[k] : DEFAULT_LINES[k]));
        const orientation = eff("orientation");
        const direction = eff("direction");
        const lineTracking = trigger.tracking || {};
        const tracking = cam.tracking || {};
        const trigger1 = cam.qr_trigger || "continuous";
        const codeType = cam.code_type || "all";
        const action = cam.qr_action || "report";
        const listId = readsCodes(cam) ? (cam.product_list_id || "") : ((state.productLists[0] || {}).id || "");
        const station = cam.station || "own";
        const readOnly = level() < 2;
        const fold = (name, title, body) => foldHtml(name, title, body, openSet.has(name));

        const cameraBody = `
            <div class="form-group"><label class="form-label">Device</label><select class="form-input" data-f="device">${deviceOptions(cam.camera_id, null)}</select>
                <div class="pl-note">Change the device this card uses. Cameras on another line are greyed out.</div></div>
            <div class="form-group"><label class="form-label">Name</label><input type="text" class="form-input" data-f="name" maxlength="128" value="${esc(row.name || "")}">
                <div class="pl-note">The camera's name everywhere (Cameras page, messages). Saved with the line setup.</div></div>
            <div class="pl-inline"><button type="button" class="btn-action btn-sm btn-outline" data-act="connect">Connect</button><button type="button" class="btn-action btn-sm btn-outline" data-act="disconnect">Disconnect</button>
                <span class="pl-note pl-conn-note" style="margin:0"></span></div>`;

        const jobBody = `
            <div class="form-group"><label class="form-label">What this camera does</label><select class="form-input" data-f="job">${Object.keys(JOBS).map((j) => option(j, JOBS[j].label, job)).join("")}</select>
                <div class="pl-note pl-job-note"></div></div>
            <div class="form-group pl-station" style="margin-bottom:0"><label class="form-label">Inspection station result</label>
                <select class="form-input" data-f="station">${option("own", "Own station: counts and rejects on its own", station)}${option("join", "Joins the product result", station)}</select>
                <div class="pl-note pl-station-note"></div></div>`;

        const modelBody = `
            <div class="form-group"><label class="form-label">Vision model</label><select class="form-input" data-f="model">${modelOptions(cam.model_id || "")}</select>
                <div class="pl-model-state" data-f="model-state"></div>
                <div class="pl-note">Cameras that pick the same model share one loaded copy. Models are uploaded on the AI models page.</div></div>
            <div class="form-group"><label class="form-label">Products to count</label><div data-kind="expected"></div>
                <div class="pl-note">The model's classes that count as good products.</div></div>
            <div class="form-group"><label class="form-label">Defects to reject</label><div data-kind="defects"></div>
                <div class="pl-note">A class can be in one list only. With nothing ticked in either list, every class the model finds counts as a product.</div></div>
            <div class="form-group"><label class="form-label">Confidence threshold</label><input type="number" class="form-input" data-f="confidence" min="0.05" max="0.99" step="0.01" placeholder="The model's own" value="${esc(numOrEmpty(cam.confidence))}">
                <div class="pl-note">Detections below this confidence are ignored (0.05 to 0.99). Empty: the model's own threshold.</div></div>
            <label class="pl-inline pl-check"><input type="checkbox" data-f="name_based_defects" ${cam.name_based_defects !== false ? "checked" : ""}> Also reject classes whose name contains "defect", "scratch" or "broken"</label>
            <div class="pl-note">Even when they are not ticked as defects. On for cameras set up before this switch existed.</div>`;

        const trackingFields = Object.entries(TRACKING).map(([k, [label, min, max, step, def]]) =>
            `<div class="form-group" style="margin:0"><label class="form-label">${label}</label><input type="number" class="form-input" data-track="${k}" min="${min}" max="${max}" step="${step}" placeholder="${esc(lineTracking[k] ?? def)}" value="${esc(numOrEmpty(tracking[k]))}"></div>`).join("");
        const countingBody = `
            <div class="pl-form-grid pl-form-tight">
                <div class="form-group"><label class="form-label">Flow direction</label><select class="form-input" data-f="flow">${Object.keys(FLOWS).map((f) => option(f, FLOWS[f].label, flowOf(orientation, direction))).join("")}</select></div>
                <div class="form-group"><label class="form-label">Count mode</label><select class="form-input" data-f="mode">${option("one", "A then B (the flow above)", direction === "both" ? "both" : "one")}${option("both", "Both ways", direction === "both" ? "both" : "one")}</select></div>
            </div>
            <div class="pl-lines-pic" data-f="lines-pic"><img alt="" hidden><canvas></canvas><span class="pl-lines-note">Drag line A or B. <button type="button" class="pl-link" data-act="picture">Take a picture</button></span></div>
            <div class="pl-form-grid pl-form-tight">
                <div class="form-group"><label class="form-label">Count line A</label><input type="number" class="form-input" data-f="line1" min="0" max="1" step="0.01" value="${esc(eff("line1_position"))}"></div>
                <div class="form-group"><label class="form-label">Count line B</label><input type="number" class="form-input" data-f="line2" min="0" max="1" step="0.01" value="${esc(eff("line2_position"))}"></div>
            </div>
            <div class="pl-note pl-lines-hint"></div>
            <details class="pl-advanced"><summary>Advanced tracking</summary>
                <div class="pl-note" style="margin:6px 0 10px">Empty: the default, shown in grey.</div>
                <div class="pl-form-grid pl-form-tight">${trackingFields}</div></details>`;

        const codesBody = `
            <div class="form-group"><label class="form-label">Code type</label><select class="form-input" data-f="code_type">${codeTypeOptions(codeType)}</select>
                <div class="pl-note">Only codes of this type are read; any other code in the picture is ignored. 2D and 1D read every type of that kind.</div></div>
            <div class="form-group"><label class="form-label">Read codes</label><select class="form-input" data-f="qr_trigger">
                ${option("continuous", "Continuously, on every frame", trigger1)}${option("line1", "One picture when a product crosses wire line 1", trigger1)}${option("line2", "One picture when a product crosses wire line 2", trigger1)}</select>
                <div class="pl-note">A picture is taken each time the counting camera sees a product cross the chosen wire line. It shows on the Line dashboard with every code outlined.</div></div>
            <div class="form-group pl-trigger-delay"><label class="form-label">Picture delay after the crossing (ms)</label>
                <input type="number" class="form-input" data-f="qr_delay" min="0" max="10000" step="10" value="${esc(cam.qr_trigger_delay_ms || 0)}">
                <div class="pl-note">For a code camera further along the belt: how long the product takes to reach it. 0 = at once.</div></div>
            <div class="form-group pl-hold"><label class="form-label">QR hold time (ms)</label>
                <input type="number" class="form-input" data-f="qr_hold" min="100" max="60000" step="100" value="${esc(cam.qr_hold_ms || 1500)}">
                <div class="pl-note">A code counts again only after it has been out of view this long.</div></div>
            <div class="form-group"><label class="form-label">Action</label><select class="form-input" data-f="qr_action">
                ${option("report", "Report only", action)}${option("accept_listed", "Accept only listed codes", action)}${option("reject_listed", "Reject listed codes", action)}</select>
                <div class="pl-note pl-action-note"></div></div>
            <div class="form-group"><label class="form-label">Product list</label><select class="form-input" data-f="qr_list">${listOptions(listId)}</select>
                <div class="pl-note">The list this camera checks its codes against. The lists are on the Products page.</div></div>
            <div class="form-group pl-no-read" style="margin-bottom:0"><label class="form-label">When no code is read</label><select class="form-input" data-f="qr_no_read">
                ${option("reject", "Reject", cam.qr_no_read || "ignore")}${option("ignore", "Ignore", cam.qr_no_read || "ignore")}</select>
                <div class="pl-note">Ignore: the product is good or bad according to the vision camera alone.</div></div>`;

        return `
            <div class="card-panel pl-card" data-key="${key}" data-camera="${esc(cam.camera_id)}">
                <div class="pl-card-head">
                    <div class="pl-card-title">
                        <span class="pl-dot pl-card-dot"></span>
                        <span class="pl-card-name">${esc(row.name || cam.camera_id)}</span>
                        <span class="pl-badge" data-f="badge">${esc(JOBS[job].short)}</span>
                    </div>
                    <div class="pl-card-tools">
                        <button type="button" class="pl-icon-btn" data-act="up" title="Move up" aria-label="Move up"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg></button>
                        <button type="button" class="pl-icon-btn" data-act="down" title="Move down" aria-label="Move down"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>
                        <button type="button" class="pl-icon-btn danger restricted-l2" data-act="remove" title="Remove from the line" aria-label="Remove from the line" ${readOnly ? "disabled" : ""}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
                    </div>
                </div>
                <div class="pl-card-sub"></div>
                <div class="pl-card-live"><img alt="Live picture"><span class="pl-card-live-note">No picture</span></div>
                <div class="pl-folds">
                    ${fold("camera", "Camera", cameraBody)}
                    ${fold("job", "Job", jobBody)}
                    ${fold("model", "AI model and classes", modelBody)}
                    ${fold("counting", "Counting", countingBody)}
                    ${fold("codes", "Codes", codesBody)}
                    ${fold("image", "Image", `<div data-f="image-form"></div>`)}
                    ${fold("video", "Video", `<div data-f="video-form"></div>`)}
                </div>
            </div>`;
    }

    // ── Class lists of a card (the model's own class names) ───────────────────

    const CLASS_FILTER_FROM = 12;  // a model with more classes than this gets a filter box

    function pickedClasses(card, kind) {
        const box = card.querySelector(`[data-kind="${kind}"]`);
        return box ? [...box.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value) : [];
    }

    function classCountNote(box) {
        const all = box.querySelectorAll('input[type="checkbox"]');
        const note = box.querySelector(".pl-class-count");
        if (!note || !all.length) return;
        const ticked = [...all].filter((input) => input.checked).map((input) => input.value);
        note.textContent = ticked.length ? `${ticked.length} of ${all.length} ticked: ${ticked.join(", ")}` : `None of ${all.length} ticked`;
    }

    function renderClassLists(card) {
        const info = setup.cards.get(card.dataset.key);
        const select = cardField(card, "model");
        if (!info || !select) return;
        const picks = info.picks;
        const modelId = select.value;
        const model = state.models.find((m) => m.id === modelId);
        const classes = model && Array.isArray(model.classes) ? model.classes.map(String) : [];
        const inModel = new Set(classes.map((c) => c.toLowerCase()));
        ["expected", "defects"].forEach((kind) => {
            const box = card.querySelector(`[data-kind="${kind}"]`);
            const ticked = new Set(picks[kind].map((c) => String(c).toLowerCase()));
            // A saved name the model does not have is shown so it can be unticked.
            const unknown = modelId && modelId === picks.savedModel ? picks[kind].filter((c) => !inModel.has(String(c).toLowerCase())) : [];
            const item = (name, checked, isUnknown) => `<label class="pl-class${isUnknown ? " unknown" : ""}" title="${esc(name)}${isUnknown ? ": not a class of this model, so it never counts" : ""}">
                <input type="checkbox" value="${esc(name)}" ${checked ? "checked" : ""}><span>${esc(name)}${isUnknown ? " (not in this model)" : ""}</span></label>`;
            if (!modelId) {
                box.innerHTML = `<div class="pl-classes"><div class="pl-classes-empty">Choose a model first: its classes are listed here.</div></div>`;
                return;
            }
            if (!classes.length && !unknown.length) {
                box.innerHTML = `<div class="pl-classes"><div class="pl-classes-empty">This model lists no class names.</div></div>`;
                return;
            }
            const known = classes.filter((c) => ticked.has(c.toLowerCase())).concat(classes.filter((c) => !ticked.has(c.toLowerCase())));
            box.innerHTML = (classes.length > CLASS_FILTER_FROM
                ? `<input type="search" class="form-input pl-class-filter" placeholder="Filter ${classes.length} classes…" aria-label="Filter classes">` : "")
                + `<div class="pl-classes" role="group">${unknown.map((c) => item(c, true, true)).join("")}${known.map((c) => item(c, ticked.has(c.toLowerCase()), false)).join("")}</div>`
                + `<div class="pl-note pl-class-count"></div>`;
            classCountNote(box);
        });
    }

    function renderModelState(card) {
        const el = cardField(card, "model-state");
        const select = cardField(card, "model");
        if (!el || !select) return;
        const dot = (color) => `<span class="pl-dot" style="background:${color}"></span>`;
        const model = state.models.find((m) => m.id === select.value);
        if (!select.value) {
            el.innerHTML = `<span style="color:var(--danger-color)">No model chosen: a vision camera cannot be saved without one.</span>`;
        } else if (!model) {
            el.innerHTML = `<span style="color:var(--danger-color)">${dot("var(--danger-color)")}This model no longer exists. Choose another one.</span>`;
        } else if (model.loaded) {
            el.innerHTML = `<span style="color:var(--success-color)">${dot("var(--success-color)")}Loaded</span>`;
        } else if (model.load_error) {
            el.innerHTML = `<span style="color:var(--danger-color)">${dot("var(--danger-color)")}Not loaded: ${esc(model.load_error)}. Nothing is detected on this camera.</span>`;
        } else {
            el.innerHTML = `${dot("var(--text-muted)")}Not loaded yet: it loads when the line setup is saved.`;
        }
    }

    function onCameraModelChange(card) {
        const info = setup.cards.get(card.dataset.key);
        if (!info) return;
        ["expected", "defects"].forEach((kind) => { info.picks[kind] = pickedClasses(card, kind); });
        renderClassLists(card);
        renderModelState(card);
    }

    const ACTION_NOTES = {
        report: "The code is shown and can fire code triggers. The product count is not changed.",
        accept_listed: "A product whose code is not in the product list is rejected. A product is good only when the vision camera found it good and its code is in the list.",
        reject_listed: "A product whose code is in the product list is rejected. A product is good only when the vision camera found it good and its code is not in the list.",
    };
    const READER_ONLY_NOTE = " This line has no vision camera, so each code read counts as one product, good or reject. The same code counts again only after it has been out of view for the hold time: counting is reliable when products pass further apart than that. For exact counts, add a vision camera.";
    const JOB_NOTES = {
        counting: "Line totals come from this camera. Exactly one vision camera counts.",
        counting_qr: "Counts and inspects with the AI model and reads codes on the same picture, for a line with one camera.",
        station: "Counts and rejects on its own and fires only the PLC and send cards that name it.",
        station_qr: "An inspection station that also reads codes on the same picture.",
        qr: "Reads codes. With a vision camera on the line, Sync pairs each product with its code.",
    };

    const setupHasVision = () => cardsOnPage().some((c) => isVisionJob(cardJob(c)));
    const setupChecksCodes = () => cardsOnPage().some((c) => JOBS[cardJob(c)].codes && cardValue(c, "qr_action") !== "report");

    // Show the folds and fields that apply to a card's job and choices.
    function syncCard(card, index) {
        const job = cardJob(card);
        const row = cameraRow(card.dataset.camera);
        card.querySelectorAll(".pl-fold").forEach((f) => { f.hidden = !foldShown(f.dataset.fold, job); });
        cardField(card, "badge").textContent = JOBS[job].short;
        cardField(card, "badge").className = `pl-badge job-${job.replace("_qr", "")}`;
        card.querySelector(".pl-card-sub").textContent = `Camera ${index + 1}${row ? ` · ${String(row.type || "").toUpperCase()}` : ""}`;
        card.querySelector(".pl-job-note").textContent = JOB_NOTES[job] || "";
        const stationShown = job === "station" || job === "station_qr";
        card.querySelector(".pl-station").hidden = !stationShown;
        card.querySelector(".pl-station-note").textContent = cardValue(card, "station") === "join"
            ? "Joins the product result: comes with the next update. Until then this camera works as an own station."
            : "Counts and rejects on its own, as a second camera always did.";
        // Codes
        const trigger = cardValue(card, "qr_trigger");
        const action = cardValue(card, "qr_action");
        const checks = JOBS[job].codes && action !== "report";
        const hasVision = setupHasVision();
        card.querySelector(".pl-trigger-delay").hidden = trigger === "continuous";
        card.querySelector(".pl-hold").hidden = trigger !== "continuous";
        // "No code was read" only exists where the line knows a product was there.
        card.querySelector(".pl-no-read").hidden = !(checks && hasVision);
        card.querySelector(".pl-action-note").textContent = (ACTION_NOTES[action] || "") + (checks && !hasVision ? READER_ONLY_NOTE : "");
        // Counting
        const flow = FLOWS[cardValue(card, "flow")] || FLOWS.down;
        const vertical = flow.orientation === "vertical";
        const a = parseFloat(cardValue(card, "line1"));
        const b = parseFloat(cardValue(card, "line2"));
        card.querySelector(".pl-lines-hint").textContent = `${vertical ? "0 = left edge, 1 = right edge" : "0 = top edge, 1 = bottom edge"} of the picture.`
            + (a >= b ? " Line A should come before line B (A smaller than B); the flow direction says which way products cross them." : "");
        renderModelState(card);
        updateConnection(card);
        drawCountLines(card);
    }

    function syncLineSetup() {
        cardsOnPage().forEach(syncCard);
        const sync = byId("plSync");
        if (sync) {
            // A camera that checks codes beside a vision camera needs Sync: it pairs each product with its code.
            const forced = setupChecksCodes() && setupHasVision();
            if (forced) sync.checked = true;
            sync.disabled = forced || level() < 2;
            byId("plSyncForced").style.display = forced ? "" : "none";
        }
        const add = byId("plAddCard");
        if (add) {
            const full = cardsOnPage().length >= MAX_CAMERAS_PER_LINE;
            add.querySelector(".pl-add-body").hidden = full;
            add.querySelector(".pl-add-full").hidden = !full;
            // The starting job offered: Counting until the line has a counting camera.
            const jobSelect = add.querySelector('[data-f="add-job"]');
            const wanted = cardsOnPage().some((c) => isCountingJob(cardJob(c))) ? "station" : "counting";
            if (jobSelect.dataset.auto !== wanted) { jobSelect.dataset.auto = wanted; jobSelect.value = wanted; }
            refreshAddDevices();
        }
    }

    function updateConnection(card) {
        const id = card.dataset.camera;
        const connected = cameraConnected(id);
        const dot = card.querySelector(".pl-card-dot");
        dot.style.background = connected ? "var(--success-color)" : "var(--danger-color)";
        dot.title = connected ? "Connected" : "Not connected";
        const note = card.querySelector(".pl-conn-note");
        if (note) note.textContent = connected ? "Connected" : "Not connected";
        const connect = card.querySelector('[data-act="connect"]');
        const disconnect = card.querySelector('[data-act="disconnect"]');
        if (connect) connect.disabled = connected || level() < 2;
        if (disconnect) disconnect.disabled = !connected || level() < 2;
    }

    // ── Count lines drawn on the camera's picture ─────────────────────────────

    function countLineGeometry(card) {
        const flow = FLOWS[cardValue(card, "flow")] || FLOWS.down;
        const both = cardValue(card, "mode") === "both";
        const clamp01 = (v, d) => { const n = parseFloat(v); return Number.isFinite(n) ? Math.min(Math.max(n, 0), 1) : d; };
        return { flow, both, a: clamp01(cardValue(card, "line1"), 0.35), b: clamp01(cardValue(card, "line2"), 0.65) };
    }

    function drawCountLines(card) {
        const box = cardField(card, "lines-pic");
        if (!box || box.closest(".pl-fold").hidden || !box.closest(".pl-fold").classList.contains("open")) return;
        const canvas = box.querySelector("canvas");
        const w = box.clientWidth;
        const h = box.clientHeight;
        if (!w || !h) return;
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(w * ratio);
        canvas.height = Math.round(h * ratio);
        const ctx = canvas.getContext("2d");
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, w, h);
        const { flow, both, a, b } = countLineGeometry(card);
        const vertical = flow.orientation === "vertical";
        const styles = getComputedStyle(document.documentElement);
        const colors = { a: styles.getPropertyValue("--primary-color").trim() || "#3b82f6", b: styles.getPropertyValue("--warning-color").trim() || "#f59e0b" };
        [["A", a, colors.a], ["B", b, colors.b]].forEach(([label, pos, color]) => {
            ctx.strokeStyle = color;
            ctx.fillStyle = color;
            ctx.lineWidth = 3;
            ctx.beginPath();
            if (vertical) { ctx.moveTo(pos * w, 0); ctx.lineTo(pos * w, h); } else { ctx.moveTo(0, pos * h); ctx.lineTo(w, pos * h); }
            ctx.stroke();
            ctx.font = "bold 13px sans-serif";
            const x = vertical ? Math.min(pos * w + 6, w - 16) : 8;
            const y = vertical ? 18 : Math.max(pos * h - 6, 14);
            ctx.fillText(label, x, y);
        });
        // An arrow in the middle shows which way products go.
        ctx.strokeStyle = "rgba(255,255,255,.85)";
        ctx.lineWidth = 2.5;
        const cx = w / 2;
        const cy = h / 2;
        const len = Math.min(w, h) * 0.18;
        const forward = flow.direction === "forward";
        const arrow = (dx, dy) => {
            const [x1, y1, x2, y2] = [cx - dx * len, cy - dy * len, cx + dx * len, cy + dy * len];
            ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            const ang = Math.atan2(y2 - y1, x2 - x1);
            ctx.beginPath();
            ctx.moveTo(x2, y2); ctx.lineTo(x2 - 10 * Math.cos(ang - 0.5), y2 - 10 * Math.sin(ang - 0.5));
            ctx.moveTo(x2, y2); ctx.lineTo(x2 - 10 * Math.cos(ang + 0.5), y2 - 10 * Math.sin(ang + 0.5));
            ctx.stroke();
        };
        const sign = forward ? 1 : -1;
        if (vertical) arrow(sign, 0); else arrow(0, sign);
        if (both) { if (vertical) arrow(-sign, 0); else arrow(0, -sign); }
    }

    function bindCountLineDrag(card) {
        const box = cardField(card, "lines-pic");
        let dragging = null;
        const posAt = (e) => {
            const r = box.getBoundingClientRect();
            const vertical = countLineGeometry(card).flow.orientation === "vertical";
            const p = vertical ? (e.clientX - r.left) / r.width : (e.clientY - r.top) / r.height;
            return Math.round(Math.min(Math.max(p, 0), 1) * 100) / 100;
        };
        box.addEventListener("pointerdown", (e) => {
            if (e.target.closest("button")) return;
            const p = posAt(e);
            const { a, b } = countLineGeometry(card);
            dragging = Math.abs(p - a) <= Math.abs(p - b) ? "line1" : "line2";
            box.setPointerCapture(e.pointerId);
            e.preventDefault();
            cardField(card, dragging).value = p.toFixed(2);
            markDirty();
            syncCard(card, cardsOnPage().indexOf(card));
        });
        box.addEventListener("pointermove", (e) => {
            if (!dragging) return;
            cardField(card, dragging).value = posAt(e).toFixed(2);
            syncCard(card, cardsOnPage().indexOf(card));
        });
        const end = () => { dragging = null; };
        box.addEventListener("pointerup", end);
        box.addEventListener("pointercancel", end);
    }

    // A still picture of the camera behind the count lines (the picture the model sees, after the ROI).
    async function loadLinesPicture(card) {
        const box = cardField(card, "lines-pic");
        const img = box.querySelector("img");
        const id = card.dataset.camera;
        try {
            const res = await fetch(`/api/v1/cameras/${encodeURIComponent(id)}/frame`, { cache: "no-store" });
            if (!res.ok) throw new Error();
            const url = URL.createObjectURL(await res.blob());
            if (card.dataset.camera !== id) { URL.revokeObjectURL(url); return; }
            if (img.dataset.blob) URL.revokeObjectURL(img.dataset.blob);
            img.dataset.blob = url;
            img.onload = () => {
                box.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
                img.hidden = false;
                drawCountLines(card);
            };
            img.src = url;
        } catch (_) {
            img.hidden = true;
            box.style.aspectRatio = "";
            drawCountLines(card);
        }
    }

    // ── Live picture in each card's header ────────────────────────────────────

    const CARD_FEED_MS = 1000;
    const setupShown = () => isTab("tabActionTrigger") && !document.hidden;

    function startCardFeed(card) {
        const info = setup.cards.get(card.dataset.key);
        if (!info || info.feed) return;
        const feed = { controller: new AbortController(), url: null };
        info.feed = feed;
        const img = card.querySelector(".pl-card-live img");
        const note = card.querySelector(".pl-card-live-note");
        const live = () => info.feed === feed && card.isConnected && setupShown();
        (async () => {
            while (live()) {
                const asked = performance.now();
                try {
                    const res = await fetch(`/api/v1/vision/annotated/camera/${encodeURIComponent(card.dataset.camera)}?max_width=320`, { cache: "no-store", signal: feed.controller.signal });
                    if (res.ok) {
                        const url = URL.createObjectURL(await res.blob());
                        if (!live()) { URL.revokeObjectURL(url); break; }
                        const previous = feed.url;
                        feed.url = url;
                        img.src = url;
                        img.classList.add("on");
                        note.textContent = "";
                        if (previous) setTimeout(() => URL.revokeObjectURL(previous), 1000);
                    } else {
                        img.classList.remove("on");
                        note.textContent = cameraConnected(card.dataset.camera) ? "No picture yet" : "Not connected";
                    }
                } catch (e) {
                    if (e.name === "AbortError") break;
                }
                await sleep(Math.max(200, CARD_FEED_MS - (performance.now() - asked)));
            }
            if (info.feed === feed) info.feed = null;
        })();
    }

    // The cards' connection dots and Connect buttons follow the cameras while the page is open.
    async function refreshSetupStatus() {
        if (!setupShown() || !byId("plCamGrid")) return;
        await Promise.all([loadCamerasList(), loadDetail().catch(() => null)]);
        cardsOnPage().forEach(updateConnection);
        startCardFeeds();
    }

    function stopCardFeeds() {
        setup.cards.forEach((info) => {
            if (!info.feed) return;
            info.feed.controller.abort();
            info.feed = null;
        });
    }

    function startCardFeeds() {
        if (!setupShown()) return;
        cardsOnPage().forEach(startCardFeed);
    }

    // ── Building the page ─────────────────────────────────────────────────────

    // Add one card to the grid (before the Add camera card) and wire it up.
    function addCard(cam, trigger, openSet) {
        const grid = byId("plCamGrid");
        const key = `k${setup.nextKey++}`;
        const row = cameraRow(cam.camera_id);
        setup.cards.set(key, {
            picks: { savedModel: cam.model_id || "", expected: [...(cam.expected_classes || [])], defects: [...(cam.defect_classes || [])] },
            cam: row,
            initialName: row ? row.name : "",
            initialSettings: null,
            feed: null,
            isNew: Boolean(cam.isNew),
        });
        grid.insertBefore(document.createRange().createContextualFragment(cameraCardHtml(key, cam, trigger, openSet)), byId("plAddCard"));
        const card = grid.querySelector(`.pl-card[data-key="${key}"]`);
        renderImageForms(card);
        renderClassLists(card);
        bindCard(card);
        if (cam.isNew) rememberFolds(card);
        // A Counting fold remembered open shows its picture at once.
        if (card.querySelector('.pl-fold[data-fold="counting"]').classList.contains("open")) requestAnimationFrame(() => loadLinesPicture(card));
        return card;
    }

    function renderImageForms(card) {
        const info = setup.cards.get(card.dataset.key);
        const row = cameraRow(card.dataset.camera);
        const camForm = { id: card.dataset.camera, type: row ? row.type : "", settings: (row && row.settings) || {} };
        const imageBox = cardField(card, "image-form");
        if (!row) {
            imageBox.innerHTML = `<div class="pl-note">This camera is not in the camera list.</div>`;
            cardField(card, "video-form").innerHTML = "";
            info.imageForm = null;
            info.initialSettings = null;
            return;
        }
        info.imageForm = CameraSettingsForm.render(imageBox, camForm, ["image"]);
        CameraSettingsForm.render(cardField(card, "video-form"), camForm, ["video"]);
        info.initialSettings = JSON.stringify(CameraSettingsForm.read(card, row.type));
        info.cam = row;
        if (card.querySelector('.pl-fold[data-fold="image"]').classList.contains("open")) info.imageForm.loadPicture(() => card.isConnected);
    }

    function bindCard(card) {
        card.addEventListener("click", (e) => {
            const head = e.target.closest(".pl-fold-head");
            if (head && card.contains(head)) {
                const fold = head.closest(".pl-fold");
                const open = !fold.classList.contains("open");
                fold.classList.toggle("open", open);
                head.setAttribute("aria-expanded", open ? "true" : "false");
                rememberFolds(card);
                if (open && fold.dataset.fold === "image") setup.cards.get(card.dataset.key)?.imageForm?.loadPicture(() => card.isConnected);
                if (open && fold.dataset.fold === "counting") loadLinesPicture(card);
                refreshSelects();
                return;
            }
            const button = e.target.closest("[data-act]");
            if (!button || !card.contains(button)) return;
            const act = button.dataset.act;
            if (act === "up" || act === "down") moveCard(card, act === "up" ? -1 : 1);
            else if (act === "remove") removeCard(card);
            else if (act === "connect" || act === "disconnect") connectCard(card, act);
            else if (act === "picture") loadLinesPicture(card);
        });
        card.addEventListener("change", (e) => {
            const t = e.target;
            if (t.matches('.pl-class input[type="checkbox"]')) {
                const own = t.closest("[data-kind]");
                // A class is a product or a defect, not both: ticking it here unticks it in the other list.
                if (t.checked) {
                    card.querySelectorAll("[data-kind]").forEach((other) => {
                        if (other === own) return;
                        other.querySelectorAll('input[type="checkbox"]').forEach((twin) => {
                            if (twin.value.toLowerCase() === t.value.toLowerCase()) twin.checked = false;
                        });
                        classCountNote(other);
                    });
                }
                classCountNote(own);
            }
            const name = t.dataset.f;
            if (name === "model") onCameraModelChange(card);
            if (name === "job") onJobChange(card, t.value);
            if (name === "device") onDeviceChange(card, t.value);
            if (name === "name") card.querySelector(".pl-card-name").textContent = t.value.trim() || card.dataset.camera;
            if (!t.classList.contains("pl-class-filter")) markDirty();
            syncLineSetup();
        });
        card.addEventListener("input", (e) => {
            const t = e.target;
            if (t.classList.contains("pl-class-filter")) {
                const text = t.value.trim().toLowerCase();
                t.parentNode.querySelectorAll(".pl-class").forEach((label) => {
                    label.hidden = Boolean(text) && !label.querySelector("input").value.toLowerCase().includes(text);
                });
                return;
            }
            markDirty();
            if (t.dataset.f === "line1" || t.dataset.f === "line2") syncCard(card, cardsOnPage().indexOf(card));
        });
        bindCountLineDrag(card);
    }

    // Exactly one counting camera on a line with a vision camera.
    function onJobChange(card, job) {
        const others = cardsOnPage().filter((c) => c !== card);
        if (isCountingJob(job)) {
            others.filter((c) => isCountingJob(cardJob(c))).forEach((c) => {
                const select = cardField(c, "job");
                select.value = cardJob(c) === "counting_qr" ? "station_qr" : "station";
                cardField(c, "station").value = "own";
                if (select._updateCustomSelectUI) select._updateCustomSelectUI();
                toast(`${c.querySelector(".pl-card-name").textContent} is now an own inspection station: a line has one counting camera.`, "info");
            });
        } else if (!others.concat([card]).some((c) => isCountingJob(cardJob(c)))) {
            // The line's totals need a counting camera: the first vision camera takes it.
            const first = cardsOnPage().find((c) => isVisionJob(cardJob(c)));
            if (first) {
                const select = cardField(first, "job");
                select.value = cardJob(first) === "station_qr" ? "counting_qr" : "counting";
                if (select._updateCustomSelectUI) select._updateCustomSelectUI();
                if (first !== card) toast(`${first.querySelector(".pl-card-name").textContent} is now the counting camera.`, "info");
                else toast("A line with a vision camera has one counting camera: this one stays the counting camera.", "info");
            }
        }
        if (isVisionJob(cardJob(card))) renderClassLists(card);
    }

    function onDeviceChange(card, id) {
        const info = setup.cards.get(card.dataset.key);
        card.dataset.camera = id;
        const row = cameraRow(id);
        info.initialName = row ? row.name : "";
        cardField(card, "name").value = row ? row.name : "";
        card.querySelector(".pl-card-name").textContent = row ? row.name : id;
        if (info.feed) { info.feed.controller.abort(); info.feed = null; }
        renderImageForms(card);
        if (card.querySelector('.pl-fold[data-fold="counting"]').classList.contains("open")) loadLinesPicture(card);
        refreshDeviceSelects();
        startCardFeed(card);
        refreshSelects();
    }

    function refreshDeviceSelects() {
        cardsOnPage().forEach((card) => {
            const select = cardField(card, "device");
            select.innerHTML = deviceOptions(card.dataset.camera, card);
            if (select._refreshCustomSelect) select._refreshCustomSelect();
        });
    }

    function moveCard(card, step) {
        const cards = cardsOnPage();
        const index = cards.indexOf(card);
        const target = cards[index + step];
        if (!target) return;
        const grid = byId("plCamGrid");
        if (step < 0) grid.insertBefore(card, target); else grid.insertBefore(target, card);
        markDirty();
        syncLineSetup();
        card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }

    function removeCard(card) {
        const name = card.querySelector(".pl-card-name").textContent;
        confirmAction("Remove camera from the line", `Remove ${name} from this line? The camera stays on the Cameras page; its settings on this line are dropped when you save.`, () => {
            const info = setup.cards.get(card.dataset.key);
            if (info && info.feed) info.feed.controller.abort();
            setup.cards.delete(card.dataset.key);
            const wasCounting = isCountingJob(cardJob(card));
            card.remove();
            if (wasCounting) {
                const first = cardsOnPage().find((c) => isVisionJob(cardJob(c)));
                if (first) {
                    cardField(first, "job").value = cardJob(first) === "station_qr" ? "counting_qr" : "counting";
                    toast(`${first.querySelector(".pl-card-name").textContent} is now the counting camera.`, "info");
                }
            }
            markDirty();
            refreshDeviceSelects();
            syncLineSetup();
            refreshSelects();
        });
    }

    async function connectCard(card, act) {
        const id = card.dataset.camera;
        const name = card.querySelector(".pl-card-name").textContent;
        try {
            await api(`/api/v1/cameras/${encodeURIComponent(id)}/${act}`, { method: "POST" });
            toast(`${name} ${act === "connect" ? "connected" : "disconnected"}`, "success");
        } catch (e) {
            toast(`${name}: ${e.message}`, "warning");
        }
        await loadCamerasList();
        try { await loadDetail(); } catch (_) { /* the dot uses the camera list */ }
        cardsOnPage().forEach(updateConnection);
        startCardFeed(card);
    }

    // ── The Add camera card ───────────────────────────────────────────────────

    function addCardHtml() {
        return `
            <div class="card-panel pl-card pl-add-card restricted-l2" id="plAddCard">
                <div class="pl-card-head"><div class="pl-card-title"><span class="pl-card-name">Add camera</span></div></div>
                <div class="pl-add-full pl-note" hidden>A line can have at most ${MAX_CAMERAS_PER_LINE} cameras. Remove one to add another.</div>
                <div class="pl-add-body">
                    <div class="form-group"><label class="form-label">Camera</label><select class="form-input" data-f="add-device"></select>
                        <div class="pl-note">Cameras on another line are greyed out.</div></div>
                    <div class="pl-inline" style="margin-bottom:12px">
                        <button type="button" class="btn-action btn-sm btn-outline" data-act="scan">Scan USB</button>
                        <button type="button" class="btn-action btn-sm btn-outline" data-act="network">Add network camera</button>
                    </div>
                    <div class="pl-add-network" hidden>
                        <div class="form-group"><label class="form-label">Name</label><input type="text" class="form-input" data-f="net-name" maxlength="64" placeholder="e.g. Infeed camera"></div>
                        <div class="form-group"><label class="form-label">Stream URL</label><input type="text" class="form-input" data-f="net-url" placeholder="rtsp://user:pass@192.168.1.100:554/stream1"></div>
                        <div class="pl-inline" style="margin-bottom:12px"><button type="button" class="btn-action btn-sm btn-blue" data-act="net-add">Add to the camera list</button></div>
                    </div>
                    <div class="form-group"><label class="form-label">Starting job</label><select class="form-input" data-f="add-job">${START_JOBS.map((j) => option(j, JOBS[j].label, "counting")).join("")}</select>
                        <div class="pl-note">The first vision camera is the counting camera. Everything can be changed on the card.</div></div>
                    <button type="button" class="btn-action btn-blue" data-act="add">Add camera</button>
                </div>
            </div>`;
    }

    function refreshAddDevices() {
        const select = document.querySelector('#plAddCard [data-f="add-device"]');
        if (!select) return;
        const owners = cameraOwners();
        const onCards = new Set(cardsOnPage().map((c) => c.dataset.camera));
        const keep = select.value;
        const free = state.cameras.filter((c) => !owners[c.id] && !onCards.has(c.id));
        const html = (free.length ? "" : `<option value="">No free camera: scan or add one</option>`) + state.cameras.map((c) => {
            const taken = owners[c.id] ? `on ${owners[c.id]}` : onCards.has(c.id) ? "on this line" : "";
            return `<option value="${esc(c.id)}" ${taken ? "disabled" : ""} ${c.id === keep && !taken ? "selected" : ""}>${esc(c.name)} (${esc(String(c.type || "").toUpperCase())})${taken ? ` · ${esc(taken)}` : ""}</option>`;
        }).join("");
        if (select.dataset.html === html) return;
        select.dataset.html = html;
        select.innerHTML = html;
        if (!select.value || select.selectedOptions[0]?.disabled) select.value = free.length ? free[0].id : "";
        if (select._refreshCustomSelect) select._refreshCustomSelect();
    }

    function bindAddCard(trigger) {
        const card = byId("plAddCard");
        card.addEventListener("click", async (e) => {
            const button = e.target.closest("[data-act]");
            if (!button) return;
            const act = button.dataset.act;
            if (act === "network") {
                const box = card.querySelector(".pl-add-network");
                box.hidden = !box.hidden;
            } else if (act === "scan") {
                button.disabled = true;
                try {
                    const found = await api("/api/v1/cameras/discover/usb");
                    await loadCamerasList();
                    toast(found && found.length ? `Found ${found.length} USB camera(s): ${found.map((c) => c.name).join(", ")}` : "No new USB camera found", found && found.length ? "success" : "info");
                    refreshDeviceSelects();
                    refreshAddDevices();
                } catch (err) { toast(`Scan failed: ${err.message}`, "warning"); }
                button.disabled = false;
            } else if (act === "net-add") {
                const name = cardField(card, "net-name").value.trim();
                const source = cardField(card, "net-url").value.trim();
                if (!name || !source) { toast("Enter the camera's name and its stream URL (rtsp:// or http://)", "warning"); return; }
                button.disabled = true;
                try {
                    // The same channel the Connections page makes for a network camera.
                    const saved = await api("/api/v1/system/endpoints", { method: "POST", body: { name, description: "", enabled: true, protocol: "ipcam", source, transport: "tcp", resolution: "640x480", buffer_size: 1 } });
                    await loadCamerasList();
                    refreshDeviceSelects();
                    refreshAddDevices();
                    const select = cardField(card, "add-device");
                    if (saved && saved.id && cameraRow(saved.id)) { select.value = saved.id; if (select._updateCustomSelectUI) select._updateCustomSelectUI(); }
                    cardField(card, "net-name").value = "";
                    cardField(card, "net-url").value = "";
                    card.querySelector(".pl-add-network").hidden = true;
                    toast(`Network camera '${name}' added to the camera list`, "success");
                } catch (err) { toast(`Could not add the camera: ${err.message}`, "warning"); }
                button.disabled = false;
            } else if (act === "add") {
                const id = cardField(card, "add-device").value;
                if (!id) { toast("Choose a camera to add (or scan for one)", "warning"); return; }
                let job = cardField(card, "add-job").value;
                const hasCounting = cardsOnPage().some((c) => isCountingJob(cardJob(c)));
                // The first vision camera added is the counting camera.
                if (isVisionJob(job) && !hasCounting) job = job.endsWith("_qr") ? "counting_qr" : "counting";
                const model = cardsOnPage().map((c) => cardValue(c, "model")).find(Boolean) || (state.models.length === 1 ? state.models[0].id : "");
                const spec = JOBS[job];
                const cam = {
                    camera_id: id, role: spec.role, counting: false, read_codes: spec.role === "vision" && spec.codes,
                    model_id: model, expected_classes: [], defect_classes: [], station: "own",
                    // Cards added now do not reject classes by their name unless asked to.
                    name_based_defects: false,
                    line1_position: DEFAULT_LINES.line1_position, line2_position: DEFAULT_LINES.line2_position,
                    orientation: DEFAULT_LINES.orientation, direction: DEFAULT_LINES.direction,
                    isNew: true,
                };
                const added = addCard(cam, trigger, new Set(["camera", "job", spec.role === "vision" ? "model" : "codes"]));
                cardField(added, "job").value = job;
                if (isCountingJob(job)) onJobChange(added, job);
                markDirty();
                refreshDeviceSelects();
                syncLineSetup();
                startCardFeed(added);
                refreshSelects();
                added.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
        });
    }

    // ── Render ────────────────────────────────────────────────────────────────

    async function renderLineSetup() {
        const page = byId("tabActionTrigger");
        if (!page) return;
        // Coming back to the page with unsaved edits of the same line: keep them.
        if (setup.dirty && setup.lineId === state.lineId && byId("plCamGrid")) {
            startCardFeeds();
            focusCard();
            return;
        }
        let card = byId("plLineSetup");
        if (!card) {
            card = document.createElement("div");
            card.id = "plLineSetup";
            page.insertBefore(card, page.firstChild);
        }
        stopCardFeeds();
        card.innerHTML = `<div class="card-panel"><div class="card-panel-header"><span class="card-panel-title">Line setup</span></div><div class="pl-note">Loading…</div></div>`;
        try {
            await Promise.all([loadDetail(), loadCamerasList(), loadModelsList(), loadCodeTypes(), loadProductLists().catch(() => [])]);
        } catch (e) {
            card.innerHTML = `<div class="card-panel"><div class="card-panel-header"><span class="card-panel-title">Line setup</span></div><div class="pl-note">Could not load the line: ${esc(e.message)}</div></div>`;
            return;
        }
        const d = state.detail;
        const trigger = d.action_trigger || {};
        const readOnly = level() < 2;
        setup.lineId = state.lineId;
        setup.dirty = false;
        setup.cards = new Map();
        card.innerHTML = `
            <div class="card-panel">
                <div class="card-panel-header">
                    <span class="card-panel-title">Line setup · ${esc(d.name)}</span>
                    <span class="pl-inline">${statePill(d.status && d.status.state)}<span class="pl-unsaved" id="plSetupDirty" hidden>Unsaved changes</span></span>
                </div>
                <div class="pl-form-grid">
                    <div class="form-group"><label class="form-label">Line name</label><input type="text" class="form-input" id="plName" maxlength="64" value="${esc(d.name)}"></div>
                    <div class="form-group"><label class="form-label">Minimum processed frames/s (0 = no alarm)</label><input type="number" class="form-input" id="plMinFps" min="0" max="240" step="1" value="${d.min_fps || 0}"></div>
                    <div class="form-group"><label class="form-label">Yield target (%)</label><input type="number" class="form-input" id="plYieldTarget" min="0" max="100" step="0.1" value="${d.yield_target || 0}">
                        <div class="pl-note">The dashboard marks the yield as below target under this value. 0 = no target.</div></div>
                    <div class="form-group"><label class="form-label">Sync products and codes</label>
                        <label class="pl-inline pl-check" style="margin-top:6px"><input type="checkbox" id="plSync" ${d.sync && d.sync.enabled ? "checked" : ""}> Pair each counted product with its code</label>
                        <div class="pl-note">For a vision camera and a code reader looking at the same spot, or one vision camera that also reads codes.</div>
                        <div class="pl-note" id="plSyncForced" style="display:none"><b>On while a camera accepts or rejects by code:</b> the line waits for the code (at most the Sync window) and then gives the product one result.</div></div>
                    <div class="form-group"><label class="form-label">Sync window (ms)</label><input type="number" class="form-input" id="plSyncWindow" min="50" max="10000" step="50" value="${(d.sync && d.sync.window_ms) || 500}">
                        <div class="pl-note">Products must pass further apart than this. A reject card's travel delay must be longer.</div></div>
                </div>
                ${(d.warnings || []).map((w) => `<div class="pl-warn" style="margin:0 0 12px">${esc(w)}</div>`).join("")}
                <div class="pl-setup-actions">
                    <button class="btn-action btn-blue restricted-l2" id="plSaveSetup" ${readOnly ? "disabled" : ""}>Save line setup</button>
                    ${d.enabled
                        ? `<button class="btn-action btn-outline restricted-l2" id="plStopLine" ${readOnly ? "disabled" : ""}>Stop line</button>`
                        : `<button class="btn-action btn-outline restricted-l2" id="plStartLine" ${readOnly ? "disabled" : ""}>Start line</button>`}
                    <button class="btn-action btn-red restricted-l2" id="plResetCounts" ${readOnly ? "disabled" : ""}>Reset counts…</button>
                    <span class="pl-note" style="margin:0">One Save for the line and every camera card. To send results to another system, add a card under Send results below.</span>
                </div>
            </div>
            <div class="pl-cam-grid" id="plCamGrid">${addCardHtml()}</div>`;
        bindAddCard(trigger);
        (d.cameras || []).forEach((cam) => addCard(cam, trigger, openFolds(cam.camera_id, [])));
        card.querySelector(".card-panel").addEventListener("input", markDirty);
        card.querySelector(".card-panel").addEventListener("change", (e) => { markDirty(); if (e.target.id === "plSync") syncLineSetup(); });
        syncLineSetup();
        byId("plSaveSetup").addEventListener("click", saveLineSetup);
        byId("plStartLine")?.addEventListener("click", () => runLine("start"));
        byId("plStopLine")?.addEventListener("click", () => runLine("stop"));
        byId("plResetCounts").addEventListener("click", () => { if (typeof confirmResetAllCounts === "function") confirmResetAllCounts(); });
        if (typeof applyRoleRestrictions === "function") applyRoleRestrictions(card);
        refreshSelects();
        startCardFeeds();
        focusCard();
    }

    async function runLine(act) {
        const go = async () => { setup.dirty = false; await lineAction(act, state.lineId); renderLineSetup(); };
        if (setup.dirty) confirmAction("Unsaved line setup", `The line setup has changes that are not saved. ${act === "start" ? "Start" : "Stop"} the line and discard them?`, go);
        else go();
    }

    // The card the Cameras page asked for: open its Camera and Image folds and scroll to it.
    function focusCard() {
        const id = setup.focusCamera;
        if (!id) return;
        const card = cardsOnPage().find((c) => c.dataset.camera === id);
        if (!card) return;
        setup.focusCamera = null;
        ["camera", "image"].forEach((name) => {
            const fold = card.querySelector(`.pl-fold[data-fold="${name}"]`);
            if (fold.classList.contains("open")) return;
            fold.classList.add("open");
            fold.querySelector(".pl-fold-head").setAttribute("aria-expanded", "true");
        });
        setup.cards.get(card.dataset.key)?.imageForm?.loadPicture(() => card.isConnected);
        setTimeout(() => {
            card.scrollIntoView({ block: "start", behavior: "smooth" });
            card.classList.add("pl-flash");
            setTimeout(() => card.classList.remove("pl-flash"), 1600);
        }, 60);
    }

    // ── Save: the line, then the cameras whose image or video settings changed ─

    function cardEntry(card) {
        const job = cardJob(card);
        const spec = JOBS[job];
        const entry = { camera_id: card.dataset.camera, role: spec.role, counting: spec.counting, qr_hold_ms: parseInt(cardValue(card, "qr_hold"), 10) || 1500 };
        if (spec.role === "vision") {
            const flow = FLOWS[cardValue(card, "flow")] || FLOWS.down;
            const num = (v) => (v === "" || v === null || v === undefined ? "" : Number(v));
            const tracking = {};
            card.querySelectorAll("[data-track]").forEach((input) => { if (input.value !== "") tracking[input.dataset.track] = Number(input.value); });
            Object.assign(entry, {
                model_id: cardValue(card, "model") || null,
                expected_classes: pickedClasses(card, "expected"),
                defect_classes: pickedClasses(card, "defects"),
                // Sent empty to go back to the model's own threshold (a key left out keeps its saved value).
                confidence: num(cardValue(card, "confidence")),
                name_based_defects: Boolean(cardField(card, "name_based_defects").checked),
                orientation: flow.orientation,
                direction: cardValue(card, "mode") === "both" ? "both" : flow.direction,
                line1_position: num(cardValue(card, "line1")),
                line2_position: num(cardValue(card, "line2")),
                tracking,
            });
            if (!spec.counting) entry.station = cardValue(card, "station") || "own";
            if (spec.codes) entry.read_codes = true;
        }
        if (spec.codes) {
            Object.assign(entry, {
                qr_trigger: cardValue(card, "qr_trigger") || "continuous",
                qr_trigger_delay_ms: Math.max(0, parseInt(cardValue(card, "qr_delay"), 10) || 0),
                code_type: cardValue(card, "code_type") || "all",
                qr_action: cardValue(card, "qr_action") || "report",
                product_list_id: cardValue(card, "qr_list") || null,
                qr_no_read: cardValue(card, "qr_no_read") || "ignore",
            });
        }
        return entry;
    }

    async function saveLineSetup() {
        const cards = cardsOnPage();
        const cameras = cards.map(cardEntry);
        const label = (i) => `Camera ${i + 1} (${cards[i].querySelector(".pl-card-name").textContent})`;
        const noModel = cameras.findIndex((c) => c.role === "vision" && !c.model_id);
        if (noModel >= 0) { toast(`${label(noModel)}: choose its vision model. A vision camera cannot be saved without one.`, "warning"); return; }
        const noList = cameras.findIndex((c) => c.qr_action && c.qr_action !== "report" && !c.product_list_id);
        if (noList >= 0) { toast(`${label(noList)}: choose the product list its codes are checked against.`, "warning"); return; }
        const body = {
            name: byId("plName").value.trim(),
            min_fps: parseFloat(byId("plMinFps").value) || 0,
            yield_target: parseFloat(byId("plYieldTarget").value) || 0,
            cameras,
            sync: { enabled: byId("plSync").checked, window_ms: parseInt(byId("plSyncWindow").value, 10) || 500 },
        };
        const button = byId("plSaveSetup");
        button.disabled = true;
        let saved;
        try {
            saved = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}`, { method: "PUT", body });
        } catch (e) {
            button.disabled = false;
            toast(`Could not save the line setup: ${e.message}`, "warning");
            return;
        }
        // Then each camera whose name, image or video settings changed.
        const failed = [];
        for (const card of cards) {
            const info = setup.cards.get(card.dataset.key);
            const row = cameraRow(card.dataset.camera);
            if (!info || !row) continue;
            const patch = {};
            const settings = CameraSettingsForm.read(card, row.type);
            if (info.initialSettings !== null && JSON.stringify(settings) !== info.initialSettings) patch.settings = settings;
            const name = cardValue(card, "name").trim();
            if (name && name !== row.name) patch.name = name;
            if (!Object.keys(patch).length) continue;
            try {
                await api(`/api/v1/cameras/${encodeURIComponent(row.id)}`, { method: "PATCH", body: patch });
            } catch (e) { failed.push(`${name || row.name}: ${e.message}`); }
        }
        setup.dirty = false;
        if (failed.length) toast(`Line saved, but camera settings were not: ${failed.join("; ")}`, "warning");
        else toast(saved.warning || "Line setup saved", saved.warning ? "warning" : "success");
        await loadLines();
        renderLineSetup();
        if (typeof checkActiveStream === "function") checkActiveStream();
    }

    window.addEventListener("beforeunload", (e) => {
        if (!setup.dirty) return;
        e.preventDefault();
        e.returnValue = "";
    });

    // Every camera's setting is on Line setup: Settings on the Cameras page opens
    // the camera's card there, for a camera that belongs to a line.
    const originalOpenCamSettings = window.openCamSettings;
    if (typeof originalOpenCamSettings === "function") {
        window.openCamSettings = function (id) {
            const line = state.lines.find((l) => (l.cameras || []).some((c) => c.camera_id === id));
            if (!line) return originalOpenCamSettings.apply(this, arguments);
            setup.focusCamera = id;
            if (line.id !== state.lineId) switchLine(line.id);
            switchTab("tabActionTrigger");
            return undefined;
        };
    }

    // ── Line Dashboard: the main feed and the camera strip ────────────────────

    // Video is fetched only while it is on screen: the Line Dashboard is open
    // and the browser tab is visible.
    const dashboardShown = () => isTab("tabDashboard") && !document.hidden;
    const lineStatusCameras = () => (state.detail && state.detail.status && state.detail.status.cameras) || [];

    // The camera shown large: the one picked in the strip, else the line's
    // counting camera, else its first vision camera, else its first camera.
    function lineFeedCamera() {
        const cams = lineStatusCameras();
        const picked = state.bigCamera[state.lineId];
        return cams.find((c) => c.camera_id === picked) || cams.find((c) => c.role === "vision" && c.counting) || cams.find((c) => c.role === "vision") || cams[0] || null;
    }

    function pauseMainFeed() {
        if (typeof stopLiveFeedStream === "function") stopLiveFeedStream();
        try { _streamPaused = true; } catch (_) { /* dashboard without pause support */ }
    }

    // The strip shows every camera but the large one, as small pictures fetched
    // one at a time (no MJPEG). The large camera alone has a stream. Together
    // the tiles stay within STRIP_FPS_BUDGET pictures a second, well inside the
    // server's per-client request limit (see TILE_MAX_FPS above).
    const STRIP_TILE_MAX_FPS = 2;
    const STRIP_FPS_BUDGET = 6;
    const STRIP_WIDTH = 320;
    const stripFeeds = new Map();  // camera id -> { img, controller, url }

    function stripInterval() {
        return 1000 / Math.min(STRIP_TILE_MAX_FPS, STRIP_FPS_BUDGET / Math.max(1, stripFeeds.size));
    }

    function startStripFeed(cameraId, img) {
        const running = stripFeeds.get(cameraId);
        if (running && running.img === img) return;
        stopStripFeed(cameraId);
        const feed = { img, controller: new AbortController(), url: null };
        stripFeeds.set(cameraId, feed);
        (async () => {
            const live = () => stripFeeds.get(cameraId) === feed && img.isConnected && dashboardShown();
            while (live()) {
                const asked = performance.now();
                let wait = 1500;
                try {
                    const res = await fetch(`/api/v1/vision/annotated/camera/${encodeURIComponent(cameraId)}?max_width=${STRIP_WIDTH}`, { cache: "no-store", signal: feed.controller.signal });
                    if (res.ok) {
                        const url = URL.createObjectURL(await res.blob());
                        if (!live()) { URL.revokeObjectURL(url); break; }
                        const previous = feed.url;
                        feed.url = url;
                        img.src = url;
                        img.closest(".pl-strip-tile")?.querySelector(".no-video-overlay")?.classList.remove("active");
                        if (previous) setTimeout(() => URL.revokeObjectURL(previous), 1000);
                        wait = Math.max(0, stripInterval() - (performance.now() - asked));
                    }
                } catch (e) {
                    if (e.name === "AbortError") break;
                }
                await sleep(wait);
            }
            if (stripFeeds.get(cameraId) === feed) stopStripFeed(cameraId);
        })();
    }

    function stopStripFeed(cameraId) {
        const feed = stripFeeds.get(cameraId);
        if (!feed) return;
        stripFeeds.delete(cameraId);
        feed.controller.abort();
        if (feed.url) setTimeout(() => URL.revokeObjectURL(feed.url), 1000);
    }

    function stopStripFeeds() {
        [...stripFeeds.keys()].forEach(stopStripFeed);
    }

    // Moves the dashboard's live video card into a row beside the camera strip,
    // or back into its grid when the line has one camera.
    function ensureFeedRow(show) {
        const page = byId("tabDashboard");
        const main = byId("liveVideoCardPanel");
        const grid = page && page.querySelector(".dashboard-grid");
        if (!page || !main || !grid) return null;
        let row = byId("plFeedRow");
        if (show) {
            if (!row) {
                row = document.createElement("div");
                row.id = "plFeedRow";
                row.className = "pl-feed-row";
                row.innerHTML = `<div class="pl-strip" id="plStrip" aria-label="The line's other cameras"></div>`;
                grid.parentNode.insertBefore(row, grid);
            }
            if (main.parentNode !== row) row.insertBefore(main, row.firstChild);
            grid.classList.add("pl-grid-single");
            return row;
        }
        if (row) {
            if (main.parentNode === row) grid.insertBefore(main, grid.firstChild);
            row.remove();
        }
        grid.classList.remove("pl-grid-single");
        return null;
    }

    const isTriggered = (cam) => Boolean(readsCodes(cam) && cam.qr_trigger && cam.qr_trigger !== "continuous");

    // A camera that reads codes shows its live picture or the last picture taken for codes.
    function secondView(cam) {
        if (!readsCodes(cam)) return "live";
        return state.secondView[cam.camera_id] || (isTriggered(cam) ? "capture" : "live");
    }

    const stripCameras = () => {
        const big = lineFeedCamera();
        return big ? lineStatusCameras().filter((c) => c.camera_id !== big.camera_id) : [];
    };

    function tileDetail(cam) {
        const limited = Boolean(cam.code_type && cam.code_type !== "all");
        if (cam.role === "qr") return `${isTriggered(cam) ? `One picture per product on wire line ${cam.qr_trigger === "line1" ? 1 : 2}` : "Reads codes continuously"}${limited ? ` · ${codeTypeLabel(cam.code_type)}` : ""}`;
        const job = cam.counting ? "Counting" : cam.station === "join" ? "Joins the product result" : "Own station";
        return `${job}${cam.read_codes ? " · reads codes" : ""}${cam.model_name ? ` · ${cam.model_name}` : ""}`;
    }

    function tileFigures(cam) {
        if (!cam.counts) return "";
        const c = cam.counts;
        return `<span>Inspected <b>${count(c.total_inspected)}</b></span><span>Good <b style="color:var(--success-color)">${count(c.good_count)}</b></span><span>Rejected <b style="color:var(--danger-color)">${count(c.rejected_count)}</b></span>`;
    }

    function renderFeedRow() {
        const cams = stripCameras();
        const types = cams.filter((c) => c.code_type && c.code_type !== "all");
        // A single type's name comes from the server's list, asked for once when first needed.
        if (types.some((c) => !state.codeTypes.some((t) => t.value === c.code_type)) && !state.codeTypesAsked) {
            state.codeTypesAsked = true;
            loadCodeTypes().then(renderFeedRow);
        }
        const signature = JSON.stringify([state.lineId, cams.map((c) => [c.camera_id, c.name || cameraName(c.camera_id), c.role, c.counting, c.station, c.read_codes, c.connected, c.qr_trigger, c.code_type, secondView(c)]), state.codeTypes.length]);
        if (!cams.length) { stopStripFeeds(); ensureFeedRow(false); return; }
        const row = ensureFeedRow(true);
        if (!row) return;
        const strip = byId("plStrip");
        // Live figures change all the time: update them in place.
        cams.forEach((cam) => {
            const figures = strip.querySelector(`.pl-strip-tile[data-camera="${CSS.escape(cam.camera_id)}"] .pl-strip-figures`);
            if (figures) setPart(figures, tileFigures(cam));
        });
        if (row.dataset.signature === signature) { ensureStripFeeds(); return; }
        row.dataset.signature = signature;
        stopStripFeeds();
        strip.classList.toggle("many", cams.length > 2);
        const canTest = level() >= 2;
        strip.innerHTML = cams.map((cam) => {
            const name = cam.name || cameraName(cam.camera_id);
            const view = secondView(cam);
            const toggle = readsCodes(cam) ? `<div class="pl-seg" data-view-of="${esc(cam.camera_id)}">
                    <button data-view="capture" class="${view === "capture" ? "active" : ""}">Last picture</button>
                    <button data-view="live" class="${view === "live" ? "active" : ""}">Live</button></div>` : "";
            const test = readsCodes(cam) && canTest ? `<button class="btn-action btn-outline pl-strip-test" data-test="${esc(cam.camera_id)}">Test picture</button>` : "";
            return `<div class="card-panel pl-strip-tile" data-camera="${esc(cam.camera_id)}">
                <div class="pl-strip-head">
                    <span class="pl-dot" style="background:${cam.connected ? "var(--success-color)" : "var(--danger-color)"}"></span>
                    <span class="pl-strip-name" title="${esc(name)}">${esc(name)}</span>
                    <span class="pl-badge">${esc(roleLabel(cam, true))}</span>
                </div>
                <div class="pl-note pl-strip-detail">${esc(tileDetail(cam))}</div>
                <button type="button" class="video-box pl-strip-pic" title="Show ${esc(name)} large" aria-label="Show ${esc(name)} large">
                    <img class="video-img" alt="${esc(name)}">
                    <div class="pl-feed-caption" style="display:none"></div>
                    <div class="no-video-overlay active"><div class="pl-strip-overlay">${cam.connected ? (view === "capture" ? "No picture yet" : "Connecting") : "No signal"}</div></div>
                </button>
                <div class="pl-row pl-strip-figures">${tileFigures(cam)}</div>
                ${toggle || test ? `<div class="pl-inline pl-strip-tools">${toggle}${test}</div>` : ""}
            </div>`;
        }).join("");
        strip.querySelectorAll(".pl-strip-pic").forEach((pic) => pic.addEventListener("click", () => {
            const id = pic.closest(".pl-strip-tile").dataset.camera;
            state.bigCamera[state.lineId] = id;
            refreshFeeds();
        }));
        strip.querySelectorAll("[data-view-of] button").forEach((b) => b.addEventListener("click", () => {
            state.secondView[b.parentNode.dataset.viewOf] = b.dataset.view;
            renderFeedRow();
            refreshLineExtras();
        }));
        strip.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", testCapture));
        state.captureShown = {};
        ensureStripFeeds();
        refreshLineExtras();
    }

    // The large camera changed: the main feed follows (the dashboard's own reader), the strip is rebuilt.
    function refreshFeeds() {
        const main = lineFeedCamera();
        if (main && main.connected && dashboardShown()) {
            activeCamId = main.camera_id;
            window.reloadStream();
        }
        window.dispatchEvent(new CustomEvent("pl:detail", { detail: state.detail }));
        renderLineExtras();
    }

    function ensureStripFeeds() {
        if (!dashboardShown()) { stopStripFeeds(); return; }
        stripCameras().forEach((cam) => {
            const img = document.querySelector(`#plStrip .pl-strip-tile[data-camera="${CSS.escape(cam.camera_id)}"] img`);
            if (img && cam.connected && secondView(cam) === "live") startStripFeed(cam.camera_id, img);
            else stopStripFeed(cam.camera_id);
        });
    }

    const QR_RESULT = {
        known: ["Known", "var(--success-color)"],
        unknown: ["Unknown", "var(--danger-color)"],
        no_read: ["No read", "var(--warning-color)"],
    };
    // Why a product was rejected (reject_reason).
    const REJECT_REASON = {
        vision_class: "vision class",
        code_not_in_list: "code not in list",
        code_in_reject_list: "code in reject list",
        no_code: "no code",
    };

    // Where a capture is shown: a strip tile of the camera that took it, or the
    // reads panel when the large camera took it.
    function tileCaptureTarget(cameraId) {
        const tile = document.querySelector(`#plStrip .pl-strip-tile[data-camera="${CSS.escape(cameraId)}"]`);
        if (!tile) return null;
        return { img: tile.querySelector("img"), caption: tile.querySelector(".pl-feed-caption"), overlay: tile.querySelector(".no-video-overlay"), shown: `tile:${cameraId}`, blob: `tile:${cameraId}` };
    }
    const inlineCaptureTarget = () => (byId("plInlineImg")
        ? { img: byId("plInlineImg"), caption: byId("plInlineCaption"), box: byId("plInlineCapture"), shown: "inline", blob: "inlineCapture" }
        : null);

    async function showCapture(capture, target) {
        if (!target || !target.img || !target.caption || !capture) return;
        state.captureShown = state.captureShown || {};
        if (state.captureShown[target.shown] === capture.id) return;
        state.captureShown[target.shown] = capture.id;
        if (target.box) target.box.hidden = false;
        const codes = capture.codes || [];
        const parts = [`<b>${capture.test ? "Test picture" : "Picture"} #${capture.id}</b>`, esc(new Date(capture.timestamp).toLocaleTimeString())];
        if (capture.wire_line) parts.push(`wire line ${capture.wire_line}`);
        if (capture.track_id !== null && capture.track_id !== undefined) parts.push(`product #${esc(capture.track_id)}`);
        if (codes.length) {
            codes.forEach((c) => {
                const [label, color] = QR_RESULT[c.known ? "known" : "unknown"];
                parts.push(`<span class="pl-code">${esc(c.code)}</span> <b style="color:${color}">${label}</b>${c.product_name ? ` ${esc(c.product_name)}` : ""}`);
            });
        } else {
            parts.push(`<b style="color:var(--warning-color)">${capture.status === "camera_offline" ? "Camera offline" : "No code read"}</b>`);
        }
        target.caption.innerHTML = parts.map((p) => `<span>${p}</span>`).join("");
        target.caption.style.display = "";
        if (capture.status === "camera_offline") return;
        try {
            const res = await fetch(`/api/v1/lines/${encodeURIComponent(state.lineId)}/qr/capture?id=${capture.id}`, { cache: "no-store" });
            if (!res.ok) return;
            const url = URL.createObjectURL(await res.blob());
            const previous = state.blobUrls[target.blob];
            state.blobUrls[target.blob] = url;
            target.img.src = url;
            if (target.overlay) target.overlay.classList.remove("active");
            if (previous) setTimeout(() => URL.revokeObjectURL(previous), 2000);
        } catch (_) { /* the next refresh tries again */ }
    }

    async function testCapture(e) {
        const button = e && e.currentTarget;
        if (button) button.disabled = true;
        try {
            const capture = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}/qr/capture`, { method: "POST" });
            const tile = stripCameras().find((c) => c.camera_id === capture.camera_id);
            if (tile && readsCodes(tile)) {
                state.secondView[tile.camera_id] = "capture";
                renderFeedRow();
                await showCapture(capture, tileCaptureTarget(tile.camera_id));
            } else {
                await showCapture(capture, inlineCaptureTarget());
            }
            toast(capture.codes && capture.codes.length ? `Test picture: ${capture.codes.map((c) => c.code).join(", ")}` : "Test picture: no code found", capture.codes && capture.codes.length ? "success" : "warning");
        } catch (err) {
            toast(`Test picture failed: ${err.message}`, "warning");
        } finally {
            if (button) button.disabled = false;
        }
    }

    function renderLineExtras() {
        const page = byId("tabDashboard");
        if (!page) return;
        renderFeedRow();
        let box = byId("plLineExtras");
        if (!box) {
            box = document.createElement("div");
            box.id = "plLineExtras";
            page.appendChild(box);
        }
        const cams = lineStatusCameras();
        const hasQr = cams.some(readsCodes);
        const reader = mainCodeReader();
        // Rebuild only when the cameras change, so the reads list does not blink.
        const signature = JSON.stringify([state.lineId, hasQr, reader && reader.camera_id]);
        if (box.dataset.signature === signature) return;
        box.dataset.signature = signature;
        if (state.captureShown) delete state.captureShown.inline;
        const testBtn = reader && level() >= 2 ? `<button class="btn-action btn-outline" id="plInlineTest" style="font-size:12px;padding:6px 12px">Test picture</button>` : "";
        box.innerHTML = hasQr
            ? `<div class="card-panel"><div class="card-panel-header"><span class="card-panel-title">Latest code reads</span>
                    <div class="pl-inline"><span id="plQrStats" style="font-size:12px;color:var(--text-muted)"></span>${testBtn}</div></div>
                ${reader ? `<div class="video-box pl-inline-capture" id="plInlineCapture" hidden>
                    <img class="video-img" id="plInlineImg" alt="Last picture taken for codes">
                    <div class="pl-feed-caption" id="plInlineCaption"></div></div>` : ""}
                <div class="table-responsive"><table class="pl-table"><thead><tr><th>Time</th><th>Code</th><th>Code check</th><th>Product</th><th>Class</th><th>Product result</th></tr></thead>
                <tbody id="plQrBody"><tr><td colspan="6">No reads yet.</td></tr></tbody></table></div></div>`
            : "";
        byId("plInlineTest")?.addEventListener("click", testCapture);
    }

    // The large camera, when it reads codes: its pictures show in the reads panel.
    function mainCodeReader() {
        const big = lineFeedCamera();
        return big && readsCodes(big) ? big : null;
    }

    async function refreshLineExtras() {
        if (!dashboardShown() || !state.detail) return;
        ensureStripFeeds();
        const body = byId("plQrBody");
        if (!body) return;
        try {
            const data = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}/qr/recent?limit=25`);
            const s = data.stats || {};
            byId("plQrStats").textContent = `Read ${s.codes_read || 0} · Unknown ${s.unknown || 0} · No read ${s.no_reads || 0} · Unpaired ${s.unpaired || 0}${s.code_rejects ? ` · Rejected by code ${s.code_rejects}` : ""}`;
            body.innerHTML = (data.reads || []).map((r) => {
                const [label, color] = QR_RESULT[r.status] || [r.status, "var(--text-muted)"];
                const paired = r.paired === false && r.status !== "no_read" ? " (unpaired)" : "";
                // The product's one result, where a camera accepts or rejects by code.
                const verdict = !r.result ? `<span style="color:var(--text-muted)">—</span>`
                    : r.result === "reject" ? `<b style="color:var(--danger-color)">Rejected</b>${r.reject_reason ? ` · ${esc(REJECT_REASON[r.reject_reason] || r.reject_reason)}` : ""}`
                        : `<b style="color:var(--success-color)">Good</b>`;
                return `<tr><td class="pl-code">${esc(new Date(r.timestamp).toLocaleTimeString())}</td><td class="pl-code">${esc(r.code || "—")}</td>
                    <td><b style="color:${color}">${esc(label)}</b>${esc(paired)}</td><td>${esc(r.product_name || "—")}</td><td>${esc(r.class_name || "—")}</td><td>${verdict}</td></tr>`;
            }).join("") || `<tr><td colspan="6">No reads yet.</td></tr>`;
            const capture = data.last_capture;
            if (capture) {
                const tile = stripCameras().find((c) => c.camera_id === capture.camera_id);
                if (tile && readsCodes(tile) && secondView(tile) === "capture") showCapture(capture, tileCaptureTarget(tile.camera_id));
                const reader = mainCodeReader();
                if (reader && capture.camera_id === reader.camera_id) showCapture(capture, inlineCaptureTarget());
            }
        } catch (_) { /* next refresh retries */ }
    }

    // The main feed shows the selected line's large camera. Only the Line
    // Dashboard shows it, so on any other page (or a hidden browser tab) no
    // stream is opened; switching back or showing the tab resumes it.
    const originalReloadStream = window.reloadStream;
    window.reloadStream = function () {
        if (!dashboardShown()) { pauseMainFeed(); return; }
        const main = lineFeedCamera();
        if (main) {
            if (!main.connected) {
                activeCamId = null;
                pauseMainFeed();
                byId("noVideoOverlay")?.classList.add("active");
                return;
            }
            // Selecting another camera on Cameras & Capture does not move the line's feed.
            activeCamId = main.camera_id;
        }
        return originalReloadStream.apply(this, arguments);
    };

    const originalCheckActiveStream = window.checkActiveStream;
    // quiet: the periodic refresh below, which reloads the stream only when the camera changed.
    window.checkActiveStream = async function (quiet) {
        let detail = null;
        try { detail = await loadDetail(); } catch (_) { detail = null; }
        if (!detail) {
            // A failed refresh keeps the current feed rather than guessing another camera.
            if (activeCamId) return;
            return originalCheckActiveStream();
        }
        const main = lineFeedCamera();
        if (!main) {
            renderLineExtras();
            return originalCheckActiveStream();
        }
        if (main.connected) {
            const changed = activeCamId !== main.camera_id;
            activeCamId = main.camera_id;
            // The server ends a feed when its camera is restarted (a new address, a
            // reconnect). The camera keeps its id, so the feed is opened again here.
            const ended = dashboardShown() && typeof isLiveFeedRunning === "function" && !isLiveFeedRunning();
            if (quiet !== true || changed || ended) window.reloadStream();
        } else {
            activeCamId = null;
            pauseMainFeed();
            byId("noVideoOverlay")?.classList.add("active");
        }
        if (isTab("tabDashboard")) renderLineExtras();
    };

    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            pauseMainFeed();
            stopStripFeeds();
            stopTileFeeds();
            stopCardFeeds();
            return;
        }
        if (isTab("tabDashboard")) {
            if (activeCamId) window.reloadStream();
            ensureStripFeeds();
            refreshLineExtras();
        } else if (isTab("tabOverview")) {
            refreshOverview();
        } else if (isTab("tabActionTrigger")) {
            startCardFeeds();
        }
    });

    // ── Products page ─────────────────────────────────────────────────────────

    function buildProductsPage() {
        ensurePage("tabProducts", "Products", `
            <div class="card-panel">
                <div class="card-panel-header">
                    <span class="card-panel-title">Product lists</span>
                    <div class="pl-inline">
                        <button class="btn-action btn-outline" id="plExport">Export CSV</button>
                        <label class="btn-action btn-blue" id="plImportLabel" style="cursor:pointer"><span id="plImportText">Import CSV as a new list</span><input type="file" id="plImportFile" accept=".csv,text/csv" style="display:none"></label>
                    </div>
                </div>
                <div class="pl-note" style="margin-top:0;margin-bottom:12px">Up to <span id="plMaxLists">4</span> lists. Each camera that reads codes checks them against the list chosen for it on Line setup. CSV files need a header row: <span class="pl-code">code,name,description</span>.</div>
                <div class="pl-inline" style="margin-bottom:6px">
                    <label class="form-label" for="plProdList" style="margin:0">List shown</label>
                    <select id="plProdList" class="form-input" aria-label="Product list shown"></select>
                    <button class="btn-action btn-outline" id="plListRename">Rename</button>
                    <label class="btn-action btn-outline" id="plReplaceLabel" style="cursor:pointer">Replace contents<input type="file" id="plReplaceFile" accept=".csv,text/csv" style="display:none"></label>
                    <button class="btn-action btn-red" id="plListDelete">Delete this list</button>
                </div>
                <div class="pl-note" id="plListUsedBy" style="margin-bottom:14px"></div>
                <div class="pl-inline" style="margin-bottom:14px">
                    <input type="text" id="plProdCode" class="form-input" maxlength="256" placeholder="Code">
                    <input type="text" id="plProdName" class="form-input" maxlength="128" placeholder="Product name">
                    <input type="text" id="plProdDesc" class="form-input" maxlength="512" placeholder="Description (optional)">
                    <button class="btn-action btn-blue" id="plProdAdd">Add</button>
                </div>
                <div class="pl-inline" style="margin-bottom:10px"><input type="search" id="plProdSearch" class="form-input" placeholder="Search code or name"><span id="plProdCount" style="font-size:12px;color:var(--text-muted)"></span></div>
                <div class="table-responsive"><table class="pl-table">
                    <thead><tr><th>Code</th><th>Name</th><th>Description</th><th>Actions</th></tr></thead>
                    <tbody id="plProdBody"><tr><td colspan="4">Loading…</td></tr></tbody>
                </table></div>
            </div>`);
        byId("plProdAdd").addEventListener("click", addProduct);
        byId("plExport").addEventListener("click", exportProducts);
        byId("plImportFile").addEventListener("change", importProducts);
        byId("plReplaceFile").addEventListener("change", replaceProductList);
        byId("plListRename").addEventListener("click", renameProductList);
        byId("plListDelete").addEventListener("click", deleteProductList);
        byId("plProdList").addEventListener("change", (e) => { showProductList(e.target.value); refreshProducts(); });
        let timer = null;
        byId("plProdSearch").addEventListener("input", (e) => {
            clearTimeout(timer);
            timer = setTimeout(() => { state.productSearch = e.target.value.trim(); refreshProducts(); }, 250);
        });
    }

    const shownProductList = () => state.productLists.find((l) => l.id === state.productListId) || null;

    function showProductList(id) {
        state.productListId = id || "";
        try { localStorage.setItem("selected_product_list", state.productListId); } catch (_) { /* private mode */ }
    }

    async function loadProductLists() {
        const data = await api("/api/v1/products/lists");
        state.productLists = data.lists || [];
        state.maxProductLists = data.max_lists || 4;
        if (!shownProductList()) showProductList(state.productLists.length ? state.productLists[0].id : "");
        return state.productLists;
    }

    // The list dropdown, the list buttons and the "2 of 4" on the import button.
    function renderProductLists() {
        const select = byId("plProdList");
        if (!select) return;
        const lists = state.productLists;
        const html = lists.map((l) => `<option value="${esc(l.id)}" ${l.id === state.productListId ? "selected" : ""}>${esc(l.name)} (${count(l.count)} code${l.count === 1 ? "" : "s"})</option>`).join("")
            || `<option value="">No product list yet</option>`;
        if (select.dataset.options !== html) {
            select.dataset.options = html;
            select.innerHTML = html;
            if (select._refreshCustomSelect) select._refreshCustomSelect(); else refreshSelects();
        }
        const canEdit = level() >= 2;
        const shown = shownProductList();
        const full = lists.length >= state.maxProductLists;
        byId("plMaxLists").textContent = state.maxProductLists;
        byId("plImportText").textContent = `Import CSV as a new list (${lists.length} of ${state.maxProductLists})`;
        const importLabel = byId("plImportLabel");
        importLabel.style.display = canEdit ? "" : "none";
        importLabel.classList.toggle("btn-blue", !full);
        importLabel.classList.toggle("btn-outline", full);
        importLabel.style.opacity = full ? "0.55" : "";
        importLabel.style.cursor = full ? "not-allowed" : "pointer";
        importLabel.title = full ? `There are already ${state.maxProductLists} lists. Delete a list first, or use Replace contents.` : "Each imported file becomes a new list";
        byId("plImportFile").disabled = full;
        ["plListRename", "plListDelete"].forEach((id) => { byId(id).disabled = !shown || !canEdit; byId(id).style.display = canEdit ? "" : "none"; });
        byId("plReplaceLabel").style.display = canEdit && shown ? "" : "none";
        byId("plExport").disabled = !shown;
        const used = (shown && shown.used_by) || [];
        byId("plListUsedBy").textContent = !shown ? "Import a CSV file to make a list, or add a first code below."
            : used.length ? `Used by: ${used.map((u) => `${u.line_name} (${u.camera_name})`).join(", ")}`
                : "Not used by any camera yet. Choose it for a camera that reads codes on Line setup.";
    }

    async function refreshProducts() {
        const body = byId("plProdBody");
        if (!body) return;
        try {
            await loadProductLists();
            renderProductLists();
            const shown = shownProductList();
            if (!shown) {
                byId("plProdCount").textContent = "";
                body.innerHTML = `<tr><td colspan="4">No product list yet.</td></tr>`;
                return;
            }
            const q = state.productSearch ? `&search=${encodeURIComponent(state.productSearch)}` : "";
            const data = await api(`/api/v1/products?list_id=${encodeURIComponent(shown.id)}&limit=500${q}`);
            const canEdit = level() >= 2;
            byId("plProdCount").textContent = `${data.total} code(s)${data.total > data.products.length ? `, showing ${data.products.length}` : ""}`;
            body.innerHTML = data.products.map((p) => `<tr data-id="${esc(p.id)}">
                <td class="pl-code">${esc(p.code)}</td><td>${esc(p.name)}</td><td style="font-size:12px;color:var(--text-muted)">${esc(p.description || "")}</td>
                <td><div class="pl-actions">${canEdit ? `<button class="btn-action btn-outline" data-act="edit">Edit</button><button class="btn-action btn-red" data-act="delete">Delete</button>` : ""}</div></td>
            </tr>`).join("") || `<tr><td colspan="4">${state.productSearch ? "No code matches the search." : "This list has no product codes yet."}</td></tr>`;
            body.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", () => {
                const row = b.closest("tr");
                const product = data.products.find((p) => p.id === row.dataset.id);
                if (b.dataset.act === "edit") editProduct(product);
                else deleteProduct(product);
            }));
        } catch (e) {
            body.innerHTML = `<tr><td colspan="4">Could not load product codes: ${esc(e.message)}</td></tr>`;
        }
    }

    async function addProduct() {
        const code = byId("plProdCode").value.trim();
        const name = byId("plProdName").value.trim();
        const description = byId("plProdDesc").value.trim();
        if (!code || !name) { toast("Enter a code and a product name", "warning"); return; }
        try {
            const shown = shownProductList();
            // Without a list yet, the server makes "List 1" for the first code.
            const added = await api("/api/v1/products", { method: "POST", body: { code, name, description, list_id: shown ? shown.id : null } });
            ["plProdCode", "plProdName", "plProdDesc"].forEach((id) => { byId(id).value = ""; });
            if (!shown) showProductList(added.list_id);
            toast(`Product code ${code} added`, "success");
            refreshProducts();
        } catch (e) { toast(`Could not add the product: ${e.message}`, "warning"); }
    }

    async function editProduct(p) {
        const name = window.prompt(`Name for ${p.code}:`, p.name);
        if (name === null) return;
        const description = window.prompt(`Description for ${p.code} (optional):`, p.description || "");
        if (description === null) return;
        try {
            await api(`/api/v1/products/${encodeURIComponent(p.id)}`, { method: "PUT", body: { name: name.trim(), description: description.trim() } });
            toast("Product updated", "success");
            refreshProducts();
        } catch (e) { toast(`Could not update the product: ${e.message}`, "warning"); }
    }

    function deleteProduct(p) {
        confirmAction("Delete product code", `Delete ${p.code} (${p.name}) from this list? Cameras that use the list will no longer find it.`, async () => {
            try {
                await api(`/api/v1/products/${encodeURIComponent(p.id)}`, { method: "DELETE" });
                toast("Product deleted", "success");
                refreshProducts();
            } catch (e) { toast(`Could not delete the product: ${e.message}`, "warning"); }
        });
    }

    async function exportProducts() {
        const shown = shownProductList();
        if (!shown) return;
        try {
            const res = await fetch(`/api/v1/products/export?list_id=${encodeURIComponent(shown.id)}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const url = URL.createObjectURL(await res.blob());
            const a = document.createElement("a");
            a.href = url;
            a.download = `${shown.name.replace(/[^A-Za-z0-9 ._()-]+/g, "_").trim() || "products"}.csv`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) { toast(`Could not export: ${e.message}`, "warning"); }
    }

    // Each imported file becomes a new list, named after the file.
    async function importProducts(e) {
        const file = e.target.files && e.target.files[0];
        e.target.value = "";
        if (!file) return;
        const form = new FormData();
        form.append("file", file);
        try {
            const r = await api("/api/v1/products/import", { method: "POST", body: form });
            showProductList(r.list.id);
            toast(`List '${r.list.name}' created with ${r.added} code(s)`, "success");
            refreshProducts();
        } catch (err) { toast(`Import failed: ${err.message}`, "warning"); }
    }

    // Updates the list that is shown from a file. The list keeps its id and
    // name, so the cameras set to it keep working.
    function replaceProductList(e) {
        const file = e.target.files && e.target.files[0];
        e.target.value = "";
        const shown = shownProductList();
        if (!file || !shown) return;
        confirmAction("Replace contents", `Replace the contents of '${shown.name}' with ${file.name}? Codes that are not in the file are removed. Cameras that use this list keep using it.`, async () => {
            const form = new FormData();
            form.append("file", file);
            try {
                const r = await api(`/api/v1/products/lists/${encodeURIComponent(shown.id)}/replace`, { method: "POST", body: form });
                toast(`'${shown.name}' replaced: ${r.added} added, ${r.updated} updated, ${r.removed} removed (${r.total} total)`, "success");
                refreshProducts();
            } catch (err) { toast(`Could not replace the contents: ${err.message}`, "warning"); }
        });
    }

    async function renameProductList() {
        const shown = shownProductList();
        if (!shown) return;
        const name = window.prompt("New name for this list:", shown.name);
        if (!name || name.trim() === shown.name) return;
        try {
            await api(`/api/v1/products/lists/${encodeURIComponent(shown.id)}`, { method: "PUT", body: { name: name.trim() } });
            toast("List renamed", "success");
            refreshProducts();
        } catch (e) { toast(`Could not rename the list: ${e.message}`, "warning"); }
    }

    function deleteProductList() {
        const shown = shownProductList();
        if (!shown) return;
        confirmAction("Delete product list", `Delete '${shown.name}' and its ${shown.count} code(s)? This cannot be undone.`, async () => {
            try {
                await api(`/api/v1/products/lists/${encodeURIComponent(shown.id)}`, { method: "DELETE" });
                showProductList("");
                toast("List deleted", "success");
                refreshProducts();
            } catch (e) { toast(`Could not delete the list: ${e.message}`, "warning"); }
        });
    }

    // ── Cameras: Line and Role columns ────────────────────────────────────────

    const originalLoadCameras = window.loadCameras;
    window.loadCameras = async function () {
        const result = await originalLoadCameras.apply(this, arguments);
        try {
            if (!state.lines.length) await loadLines();
            const owner = {};
            state.lines.forEach((l) => (l.cameras || []).forEach((c) => { owner[c.camera_id] = { line: l.name, role: c.role, read_codes: c.read_codes }; }));
            const table = byId("cameraTable");
            const headRow = table && table.querySelector("thead tr");
            if (headRow && !headRow.querySelector(".pl-th")) {
                headRow.insertAdjacentHTML("beforeend", `<th class="pl-th">Line</th><th class="pl-th">Role</th>`);
            }
            table?.querySelectorAll("tbody tr").forEach((row) => {
                if (row.querySelector(".pl-td")) return;
                const single = row.querySelector("td[colspan]");
                if (single) { single.colSpan = Number(single.colSpan) + 2; return; }
                const o = owner[row.dataset.id];
                row.insertAdjacentHTML("beforeend", `<td class="pl-td" style="font-size:12px">${o ? esc(o.line) : '<span style="color:var(--text-muted)">—</span>'}</td>
                    <td class="pl-td" style="font-size:12px">${o ? roleLabel(o) : ""}</td>`);
            });
        } catch (_) { /* the table works without the extra columns */ }
        return result;
    };

    // ── Communications: "Used by" ─────────────────────────────────────────────

    const originalRenderCommsGrid = window.renderCommsGrid;
    let endpointsFetchedAt = 0;
    window.renderCommsGrid = function () {
        const result = originalRenderCommsGrid.apply(this, arguments);
        const annotate = () => document.querySelectorAll("#commsGrid .comm-card[data-ep-id]").forEach((card) => {
            const ep = state.endpoints.find((e) => e.id === card.dataset.epId);
            const used = (ep && ep.used_by) || [];
            let row = card.querySelector(".pl-used-by");
            if (!row) {
                row = document.createElement("div");
                row.className = "pl-used-by pl-note";
                (card.querySelector(".comm-card-details") || card).insertAdjacentElement("afterend", row);
            }
            row.textContent = used.length ? `Used by: ${used.join(", ")}` : "Used by: no line";
        });
        if (Date.now() - endpointsFetchedAt > 3000) {
            endpointsFetchedAt = Date.now();
            api("/api/v1/system/endpoints").then((eps) => { state.endpoints = eps || []; annotate(); }).catch(() => {});
        } else {
            annotate();
        }
        return result;
    };

    // ── Audit Logs: Line column and filter ────────────────────────────────────

    function auditCategoryClass(category) {
        const c = String(category || "");
        if (c.includes("AUTH")) return "badge-cat-auth";
        if (c.includes("CAMERAS")) return "badge-cat-cameras";
        if (c.includes("USERS")) return "badge-cat-users";
        return "badge-cat-comms";
    }

    // Same table as before, plus a Line column and a line filter.
    window.loadAuditLogs = async function () {
        decorateAuditTable();
        const tbody = byId("auditTableBody");
        if (!tbody) return;
        try {
            const filter = state.auditLine ? `&line_id=${encodeURIComponent(state.auditLine)}` : "";
            const logs = await api(`/api/v1/system/audit-logs?limit=100${filter}`);
            tbody.innerHTML = logs.map((l) => `<tr>
                <td data-label="Timestamp" style="font-family:monospace;font-size:11px;color:var(--text-muted)">${esc(new Date(l.timestamp).toLocaleString())}</td>
                <td data-label="User Account"><b>${esc(l.username)}</b></td>
                <td data-label="Clearance"><span class="clearance-pill pill-lvl-${Number(l.clearance_level) || 1}">L${Number(l.clearance_level) || 1}: ${esc(l.role)}</span></td>
                <td data-label="Category"><span class="badge-audit ${auditCategoryClass(l.category)}">${esc(l.category)}</span></td>
                <td data-label="Details" style="font-size:12px">${esc(l.details)}</td>
                <td data-label="Line" style="font-size:12px">${esc(l.line_id ? lineName(l.line_id) : "")}</td></tr>`).join("")
                || `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:18px">No activity logged${state.auditLine ? " for this line" : " yet"}.</td></tr>`;
        } catch (_) { /* keep the last view */ }
    };

    function decorateAuditTable() {
        const tbody = byId("auditTableBody");
        const table = tbody && tbody.closest("table");
        if (!table) return;
        const headRow = table.querySelector("thead tr");
        if (headRow && !headRow.querySelector(".pl-th")) headRow.insertAdjacentHTML("beforeend", `<th class="pl-th">Line</th>`);
        const panelHeader = table.closest(".card-panel")?.querySelector(".card-panel-header");
        if (panelHeader && !byId("plAuditLine")) {
            panelHeader.insertAdjacentHTML("beforeend", `<div class="pl-inline" style="font-size:12px;color:var(--text-muted)">Line
                <select id="plAuditLine" class="form-input" style="min-width:150px" aria-label="Show the audit log of one line"></select></div>`);
            byId("plAuditLine").addEventListener("change", (e) => { state.auditLine = e.target.value; window.loadAuditLogs(); });
            refreshSelects();
        }
        const select = byId("plAuditLine");
        if (select) {
            const html = `<option value="">All lines</option>` + state.lines.map((l) => `<option value="${esc(l.id)}" ${state.auditLine === l.id ? "selected" : ""}>${esc(l.name)}</option>`).join("");
            if (select.dataset.options !== html) {
                select.dataset.options = html;
                select.innerHTML = html;
                if (select._refreshCustomSelect) select._refreshCustomSelect();
            }
        }
    }

    // ── Menu and tab hooks ────────────────────────────────────────────────────

    const MENU_ICONS = {
        tabOverview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
        tabLines: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="8" cy="6" r="1.5"/><circle cx="14" cy="12" r="1.5"/><circle cx="10" cy="18" r="1.5"/></svg>',
        tabProducts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5v14M7 5v14M11 5v14M14 5v14M18 5v14M21 5v14"/></svg>',
    };

    function menuItem(tab, label) {
        const li = document.createElement("li");
        li.className = "menu-item";
        li.id = `plMenu_${tab}`;
        li.setAttribute("onclick", `switchTab('${tab}')`);
        li.innerHTML = `<span class="menu-icon">${MENU_ICONS[tab]}</span><span>${label}</span>`;
        return li;
    }

    // Plant overview opens the "Run" group; Lines and Products open "Setup".
    function buildMenu() {
        const dashItem = [...document.querySelectorAll(".menu-item")].find((m) => (m.getAttribute("onclick") || "").includes("'tabDashboard'"));
        const logicItem = [...document.querySelectorAll(".menu-item")].find((m) => (m.getAttribute("onclick") || "").includes("'tabActionTrigger'"));
        if (!dashItem || byId("plMenu_tabOverview")) return;
        dashItem.parentNode.insertBefore(menuItem("tabOverview", "Plant overview"), dashItem);
        const after = byId("navGroupSetup") || logicItem || dashItem;
        const lines = menuItem("tabLines", "Lines");
        const products = menuItem("tabProducts", "Products");
        after.insertAdjacentElement("afterend", lines);
        lines.insertAdjacentElement("afterend", products);
        applyRoleVisibility();
    }

    function applyRoleVisibility() {
        const supervisor = level() >= 2;
        ["tabLines", "tabProducts"].forEach((tab) => {
            const item = byId(`plMenu_${tab}`);
            if (item) item.style.display = supervisor ? "" : "none";
        });
    }

    function stopTimers() {
        Object.values(state.timers).forEach(clearInterval);
        state.timers = {};
    }

    const originalSwitchTab = window.switchTab;
    window.switchTab = function (id) {
        // Lines and Products are for Supervisor and Admin.
        if ((id === "tabLines" || id === "tabProducts") && level() < 2) id = "tabOverview";
        const result = originalSwitchTab.call(this, id);
        enterTab(id);
        return result;
    };

    // Start what a page needs (refresh timers, feeds) and stop what the others used.
    function enterTab(id) {
        stopTimers();
        applyRoleVisibility();
        if (id !== "tabDashboard") {
            // No page but the Line Dashboard shows these feeds: close them, even
            // one that has not delivered its first frame yet.
            pauseMainFeed();
            stopStripFeeds();
        }
        if (id !== "tabActionTrigger") stopCardFeeds();
        if (id !== "tabOverview") stopTileFeeds();
        if (id === "tabOverview") {
            loadCamerasList().then(refreshOverview);
            state.timers.overview = setInterval(refreshOverview, 1500);
        } else if (id === "tabLines") {
            refreshLinesPage();
            state.timers.lines = setInterval(() => { if (!document.activeElement || document.activeElement.id !== "plNewLineName") refreshLinesPage(); }, 5000);
        } else if (id === "tabProducts") {
            refreshProducts();
        } else if (id === "tabActionTrigger") {
            renderLineSetup();
            state.timers.setup = setInterval(refreshSetupStatus, 5000);
        } else if (id === "tabDashboard") {
            loadDetail().then(() => { renderLineExtras(); refreshLineExtras(); window.checkActiveStream(true); }).catch(() => {});
            state.timers.extras = setInterval(refreshLineExtras, 1000);
            state.timers.lineCams = setInterval(() => window.checkActiveStream(true), 5000);
        }
    }

    const activeTabId = () => document.querySelector(".tab-page.active")?.id;

    // ── Start ─────────────────────────────────────────────────────────────────

    function start() {
        buildMenu();
        buildOverviewPage();
        buildLinesPage();
        buildProductsPage();
        loadLines().then(() => {
            if (state.lineId !== PRIMARY) {
                // The dashboard loaded Line 1's settings before the lines list came in.
                if (typeof loadServerSettings === "function") loadServerSettings();
                if (typeof loadPlcActionCards === "function") loadPlcActionCards();
                if (typeof checkActiveStream === "function") checkActiveStream();
            }
            const tab = activeTabId();
            if (tab) enterTab(tab);
        }).catch(() => { /* not signed in yet; retried after login */ });
    }

    // Re-read lines after sign-in (the dashboard calls initDashboard then).
    const originalInitDashboard = window.initDashboard;
    if (typeof originalInitDashboard === "function") {
        window.initDashboard = function () {
            const result = originalInitDashboard.apply(this, arguments);
            applyRoleVisibility();
            loadLines().then(() => {
                if (typeof checkActiveStream === "function") checkActiveStream();
                // The page shown after sign-in did not come through switchTab.
                const tab = activeTabId();
                if (tab) enterTab(tab);
            }).catch(() => {});
            return result;
        };
    }

    window.ProductionLines = {
        get lineId() { return state.lineId; },
        get detail() { return state.detail; },
        lineName: (id) => lineName(id || state.lineId),
        // "start" | "stop" | "logic" | ... for the selected line, with the same messages as the Lines page.
        lineAction: (act, id) => lineAction(act, id || state.lineId),
        refreshDetail: () => loadDetail(),
        lineCameras: () => ((state.detail && state.detail.cameras) || []).map((c) => ({ ...c, name: cameraName(c.camera_id) })),
        // The camera the Line dashboard shows large (picked in the camera strip, else the counting camera).
        feedCameraId: () => (lineFeedCamera() || {}).camera_id || null,
        reload: loadLines,
    };

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();
