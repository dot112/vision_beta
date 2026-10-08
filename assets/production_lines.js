/*
 * Production lines (version 2) for the dashboard (/dashboard).
 *
 * Loaded after the dashboard's own script and styled with the classes and
 * colour variables of assets/dashboard.css. It adds:
 *   - a line selector in the header; Line dashboard and Line setup follow it
 *     and each browser remembers its choice;
 *   - the Plant overview, Lines and Products pages;
 *   - the line and cameras card (cameras, roles, Sync, model) on Line setup,
 *     a second camera feed and a list of recent QR reads on Line dashboard;
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
        captureShown: null,   // id of the QR picture on screen
        inlineCaptureShown: null,  // the same, in the reads panel (a main camera that reads codes)
        feedRetryAt: 0,
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
        .pl-feed-row { display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-bottom:20px; }
        .pl-feed-row > .card-panel { margin-bottom:0; min-width:0; }
        .dashboard-grid.pl-grid-single { grid-template-columns:1fr; }
        .pl-inline-capture { position:relative; margin-bottom:12px; max-width:640px; }
        .pl-inline-capture[hidden] { display:none; }
        .pl-feed-caption { position:absolute; left:0; right:0; bottom:0; z-index:5; display:flex; flex-wrap:wrap; gap:4px 14px; padding:8px 12px; font-size:12px; color:#fff; background:rgba(0,0,0,.62); }
        .pl-seg { display:inline-flex; border:1px solid var(--border-color); border-radius:6px; overflow:hidden; }
        .pl-seg button { background:transparent; border:0; padding:5px 11px; font-size:11px; font-weight:600; color:var(--text-muted); cursor:pointer; }
        .pl-seg button.active { background:var(--primary-color); color:#fff; }
        @media (max-width:992px) { .pl-feed-row { grid-template-columns:1fr; } }
        @media (max-width:700px) { .pl-line-select { margin-left:8px; } .pl-line-select > span { display:none; } .pl-stats { grid-template-columns:repeat(2, 1fr); } }
    `;
    document.head.appendChild(style);

    // ── Per-line scoping of the existing dashboard requests ───────────────────

    // Browser-side caches the dashboards keep; each line gets its own copy.
    const PER_LINE_STORAGE = new Set(["plc_action_trigger_cards", "action_trigger_cfg"]);
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
        if (pending) {
            confirmAction("Unsaved card changes", "The PLC action or send cards of this line have changes that are not applied. Switch lines and discard them?", go);
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

    // ── Line setup card on Line Logic ─────────────────────────────────────────

    function cameraBoxHtml(index, cam) {
        const options = [`<option value="">— None —</option>`].concat(state.cameras.map((c) =>
            `<option value="${esc(c.id)}" ${cam && cam.camera_id === c.id ? "selected" : ""}>${esc(c.name)} (${esc(String(c.type).toUpperCase())})</option>`));
        // The role choice: "vision_qr" is a vision camera that also reads codes
        // (saved as role "vision" with read_codes on).
        const role = !cam ? "vision" : cam.role === "qr" ? "qr" : (cam.read_codes ? "vision_qr" : "vision");
        const trigger = (cam && cam.qr_trigger) || "continuous";
        const codes = role !== "vision";
        const qrOnly = codes ? "" : "display:none";
        const holdShown = codes && trigger === "continuous" ? "" : "display:none";
        const delayShown = codes && trigger !== "continuous" ? "" : "display:none";
        const triggerOption = (value, label) => `<option value="${value}" ${trigger === value ? "selected" : ""}>${label}</option>`;
        const codeType = (cam && cam.code_type) || "all";
        const codeTypes = state.codeTypes.some((t) => t.value === codeType)
            ? state.codeTypes
            : state.codeTypes.concat([{ value: codeType, label: codeType, kind: "saved" }]);
        const KIND_PREFIX = { "2d": "2D · ", "1d": "1D · " };
        const codeTypeOptions = codeTypes.map((t) =>
            `<option value="${esc(t.value)}" ${t.value === codeType ? "selected" : ""}>${esc((KIND_PREFIX[t.kind] || "") + t.label)}</option>`).join("");
        // What the camera does with each code, and the product list it checks against.
        const action = (cam && cam.qr_action) || "report";
        const noRead = (cam && cam.qr_no_read) || "ignore";
        const actionOption = (value, label) => `<option value="${value}" ${action === value ? "selected" : ""}>${label}</option>`;
        // A camera that reads codes for the first time starts on the first list.
        const listId = cam && readsCodes(cam) ? (cam.product_list_id || "") : ((state.productLists[0] || {}).id || "");
        const lists = state.productLists.some((l) => l.id === listId) || !listId
            ? state.productLists
            : state.productLists.concat([{ id: listId, name: "(deleted list)", count: 0 }]);
        const listOptions = [`<option value="" ${listId ? "" : "selected"}>— None —</option>`].concat(lists.map((l) =>
            `<option value="${esc(l.id)}" ${l.id === listId ? "selected" : ""}>${esc(l.name)} (${count(l.count)} code${l.count === 1 ? "" : "s"})</option>`)).join("");
        // The model that runs on a vision camera; its two class lists are filled from that model (renderClassLists).
        const modelId = cam && cam.role === "vision" ? (cam.model_id || "") : "";
        const models = !modelId || state.models.some((m) => m.id === modelId)
            ? state.models
            : state.models.concat([{ id: modelId, name: "(deleted model)", version: "", classes: [] }]);
        const modelOptions = [`<option value="" ${modelId ? "" : "selected"}>— Choose a model —</option>`].concat(models.map((m) =>
            `<option value="${esc(m.id)}" ${m.id === modelId ? "selected" : ""}>${esc(m.name)} ${esc(m.version || "")}</option>`)).join("");
        const visionOnly = role === "qr" ? "display:none" : "";
        return `
            <div class="pl-cam-box" data-index="${index}">
                <h4>Camera ${index + 1}</h4>
                <div class="form-group"><label class="form-label">Camera</label><select class="form-input" id="plCam${index}">${options.join("")}</select></div>
                <div class="form-group"><label class="form-label">Role</label><select class="form-input pl-role" id="plRole${index}">
                    <option value="vision" ${role === "vision" ? "selected" : ""}>Vision (counting and inspection)</option>
                    <option value="qr" ${role === "qr" ? "selected" : ""}>QR code / barcode reader</option>
                    <option value="vision_qr" ${role === "vision_qr" ? "selected" : ""}>Vision + QR code / barcode reader</option>
                </select>
                    <div class="pl-note pl-vision-qr-note" style="${role === "vision_qr" ? "" : "display:none"}">Counts and inspects with the AI model and reads codes on the same picture, for a line with one camera.</div></div>
                <div class="form-group pl-vision-only" style="${visionOnly}"><label class="form-label">Vision model</label><select class="form-input pl-model" id="plCamModel${index}">${modelOptions}</select>
                    <div class="pl-model-state" id="plModelState${index}"></div>
                    <div class="pl-note">The model that runs on this camera. Cameras that pick the same model share one loaded copy. Models are uploaded on the AI models page.</div></div>
                <div class="form-group pl-vision-only" style="${visionOnly}"><label class="form-label">Products to count</label>
                    <div id="plExpected${index}" data-kind="expected"></div>
                    <div class="pl-note">The model's classes that count as good products.</div></div>
                <div class="form-group pl-vision-only" style="${visionOnly}"><label class="form-label">Defects to reject</label>
                    <div id="plDefects${index}" data-kind="defects"></div>
                    <div class="pl-note">The model's classes that reject the product. A class can be in one list only. With nothing ticked in either list, every class the model finds counts as a product.</div></div>
                <div class="form-group pl-qr-only" style="${qrOnly}"><label class="form-label">Code type</label><select class="form-input" id="plCodeType${index}">${codeTypeOptions}</select>
                    <div class="pl-note">Only codes of this type are read; any other code in the picture is ignored. 2D and 1D read every type of that kind.</div></div>
                <div class="form-group pl-qr-only" style="${qrOnly}"><label class="form-label">Read codes</label><select class="form-input pl-trigger" id="plTrig${index}">
                    ${triggerOption("continuous", "Continuously, on every frame")}
                    ${triggerOption("line1", "One picture when a product crosses wire line 1")}
                    ${triggerOption("line2", "One picture when a product crosses wire line 2")}
                </select>
                    <div class="pl-note">A picture is taken each time the counting camera sees a product cross the chosen wire line. It shows on the Line dashboard with every code outlined.</div></div>
                <div class="form-group pl-qr-only" style="${qrOnly}"><label class="form-label">Action</label><select class="form-input pl-qr-action" id="plQrAction${index}">
                    ${actionOption("report", "Report only")}
                    ${actionOption("accept_listed", "Accept only listed codes")}
                    ${actionOption("reject_listed", "Reject listed codes")}
                </select>
                    <div class="pl-note pl-action-note"></div></div>
                <div class="form-group pl-qr-only" style="${qrOnly}"><label class="form-label">Product list</label><select class="form-input" id="plQrList${index}">${listOptions}</select>
                    <div class="pl-note">The list this camera checks its codes against. The lists are on the Products page.</div></div>
                <div class="form-group pl-no-read" style="display:none"><label class="form-label">When no code is read</label><select class="form-input" id="plQrNoRead${index}">
                    <option value="reject" ${noRead === "reject" ? "selected" : ""}>Reject</option>
                    <option value="ignore" ${noRead === "ignore" ? "selected" : ""}>Ignore</option>
                </select>
                    <div class="pl-note">Ignore: the product is good or bad according to the vision camera alone.</div></div>
                <div class="form-group pl-trigger-delay" style="margin-bottom:0;${delayShown}"><label class="form-label">Picture delay after the crossing (ms)</label>
                    <input type="number" class="form-input" id="plTrigDelay${index}" min="0" max="10000" step="10" value="${cam && cam.qr_trigger_delay_ms ? cam.qr_trigger_delay_ms : 0}">
                    <div class="pl-note">For a QR camera further along the belt: how long the product takes to reach it. 0 = at once.</div></div>
                <div class="form-group pl-hold" style="margin-bottom:0;${holdShown}"><label class="form-label">QR hold time (ms)</label>
                    <input type="number" class="form-input" id="plHold${index}" min="100" max="60000" step="100" value="${cam && cam.qr_hold_ms ? cam.qr_hold_ms : 1500}">
                    <div class="pl-note">A code counts again only after it has been out of view this long.</div>
                </div>
            </div>`;
    }

    // What is ticked in each camera box's two class lists: [{ savedModel, expected: [names], defects: [names] }].
    // Set from the saved line when Line setup is drawn, and read back from the page when a box's model changes.
    let setupPicks = [];
    const CLASS_BOX = { expected: "plExpected", defects: "plDefects" };
    const CLASS_FILTER_FROM = 12;  // a model with more classes than this gets a filter box

    function pickedClasses(index, kind) {
        const box = byId(`${CLASS_BOX[kind]}${index}`);
        return box ? [...box.querySelectorAll('input[type="checkbox"]:checked')].map((input) => input.value) : [];
    }

    function classCountNote(box) {
        const all = box.querySelectorAll('input[type="checkbox"]');
        const note = box.querySelector(".pl-class-count");
        if (!note || !all.length) return;
        const ticked = [...all].filter((input) => input.checked).map((input) => input.value);
        note.textContent = ticked.length ? `${ticked.length} of ${all.length} ticked: ${ticked.join(", ")}` : `None of ${all.length} ticked`;
    }

    // The two class lists of a camera box, filled from the chosen model's own class names.
    function renderClassLists(index) {
        const picks = setupPicks[index];
        const select = byId(`plCamModel${index}`);
        if (!picks || !select) return;
        const modelId = select.value;
        const model = state.models.find((m) => m.id === modelId);
        const classes = model && Array.isArray(model.classes) ? model.classes.map(String) : [];
        const inModel = new Set(classes.map((c) => c.toLowerCase()));
        Object.keys(CLASS_BOX).forEach((kind) => {
            const box = byId(`${CLASS_BOX[kind]}${index}`);
            const ticked = new Set(picks[kind].map((c) => String(c).toLowerCase()));
            // A saved name the model does not have (typed before the lists were picked) is shown so it can be unticked.
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
            // Ticked classes first, then the rest in the model's order.
            const known = classes.filter((c) => ticked.has(c.toLowerCase())).concat(classes.filter((c) => !ticked.has(c.toLowerCase())));
            box.innerHTML = (classes.length > CLASS_FILTER_FROM
                ? `<input type="search" class="form-input pl-class-filter" placeholder="Filter ${classes.length} classes…" aria-label="Filter classes">` : "")
                + `<div class="pl-classes" role="group">${unknown.map((c) => item(c, true, true)).join("")}${known.map((c) => item(c, ticked.has(c.toLowerCase()), false)).join("")}</div>`
                + `<div class="pl-note pl-class-count"></div>`;
            classCountNote(box);
        });
    }

    // Whether the model chosen in a camera box is in memory.
    function renderModelState(index) {
        const el = byId(`plModelState${index}`);
        const select = byId(`plCamModel${index}`);
        if (!el || !select) return;
        const dot = (color) => `<span class="pl-dot" style="background:${color}"></span>`;
        const modelId = select.value;
        const model = state.models.find((m) => m.id === modelId);
        if (!byId(`plCam${index}`).value) {
            el.textContent = "";  // no camera in this box yet
        } else if (!modelId) {
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

    function onCameraModelChange(index) {
        const picks = setupPicks[index];
        if (!picks) return;
        Object.keys(CLASS_BOX).forEach((kind) => { picks[kind] = pickedClasses(index, kind); });
        renderClassLists(index);
        renderModelState(index);
    }

    const ACTION_NOTES = {
        report: "The code is shown and can fire code triggers. The product count is not changed.",
        accept_listed: "A product whose code is not in the product list is rejected. A product is good only when the vision camera found it good and its code is in the list.",
        reject_listed: "A product whose code is in the product list is rejected. A product is good only when the vision camera found it good and its code is not in the list.",
    };
    const READER_ONLY_NOTE = " This line has no vision camera, so each code read counts as one product, good or reject. The same code counts again only after it has been out of view for the hold time: counting is reliable when products pass further apart than that. For exact counts, add a vision camera.";

    // What the camera boxes say about the line as a whole.
    function setupBoxes() {
        return [0, 1].map((i) => ({
            index: i,
            camera: byId(`plCam${i}`) ? byId(`plCam${i}`).value : "",
            role: byId(`plRole${i}`) ? byId(`plRole${i}`).value : "vision",
            action: byId(`plQrAction${i}`) ? byId(`plQrAction${i}`).value : "report",
        })).filter((b) => b.camera);
    }
    const setupHasVision = () => setupBoxes().some((b) => b.role !== "qr");
    const setupChecksCodes = () => setupBoxes().some((b) => b.role !== "vision" && b.action !== "report");

    // Show only the fields that apply to each camera box's role and capture choice.
    function syncCameraBox(box) {
        const index = box.dataset.index;
        const role = byId(`plRole${index}`).value;
        const trigger = byId(`plTrig${index}`).value;
        const action = byId(`plQrAction${index}`).value;
        const codes = role !== "vision";
        const checks = codes && action !== "report";
        const hasVision = setupHasVision();
        box.querySelector(".pl-vision-qr-note").style.display = role === "vision_qr" ? "" : "none";
        box.querySelectorAll(".pl-qr-only").forEach((el) => { el.style.display = codes ? "" : "none"; });
        box.querySelectorAll(".pl-vision-only").forEach((el) => { el.style.display = role === "qr" ? "none" : ""; });
        renderModelState(Number(index));
        box.querySelector(".pl-trigger-delay").style.display = codes && trigger !== "continuous" ? "" : "none";
        box.querySelector(".pl-hold").style.display = codes && trigger === "continuous" ? "" : "none";
        // "No code was read" only exists where the line knows a product was
        // there: it has a vision camera that counts the product.
        box.querySelector(".pl-no-read").style.display = checks && hasVision ? "" : "none";
        box.querySelector(".pl-action-note").textContent = (ACTION_NOTES[action] || "") + (checks && !hasVision ? READER_ONLY_NOTE : "");
    }

    // The fields that depend on both camera boxes: each box, and Sync.
    function syncLineSetup() {
        document.querySelectorAll("#plLineSetup .pl-cam-box").forEach(syncCameraBox);
        const sync = byId("plSync");
        if (!sync) return;
        // A camera that checks codes beside a vision camera needs Sync: it is
        // what pairs each product with its code.
        const forced = setupChecksCodes() && setupHasVision();
        if (forced) sync.checked = true;
        sync.disabled = forced;
        const note = byId("plSyncForced");
        if (note) note.style.display = forced ? "" : "none";
    }

    async function renderLineSetup() {
        const page = byId("tabActionTrigger");
        if (!page) return;
        let card = byId("plLineSetup");
        if (!card) {
            card = document.createElement("div");
            card.id = "plLineSetup";
            card.className = "card-panel";
            page.insertBefore(card, page.firstChild);
        }
        card.innerHTML = `<div class="card-panel-header"><span class="card-panel-title">Line setup</span></div><div class="pl-note">Loading…</div>`;
        try {
            await Promise.all([loadDetail(), loadCamerasList(), loadModelsList(), loadCodeTypes(), loadProductLists().catch(() => [])]);
        } catch (e) {
            card.innerHTML = `<div class="card-panel-header"><span class="card-panel-title">Line setup</span></div><div class="pl-note">Could not load the line: ${esc(e.message)}</div>`;
            return;
        }
        const d = state.detail;
        const cams = d.cameras || [];
        setupPicks = [0, 1].map((i) => {
            const cam = cams[i] && cams[i].role === "vision" ? cams[i] : {};
            return { savedModel: cam.model_id || "", expected: [...(cam.expected_classes || [])], defects: [...(cam.defect_classes || [])] };
        });
        const countingIdx = cams.findIndex((c) => c.role === "vision" && c.counting);
        const readOnly = level() < 2;
        card.innerHTML = `
            <div class="card-panel-header">
                <span class="card-panel-title">Line setup · ${esc(d.name)}</span>
                <span>${statePill(d.status && d.status.state)}</span>
            </div>
            <div class="pl-form-grid">
                <div class="form-group"><label class="form-label">Line name</label><input type="text" class="form-input" id="plName" maxlength="64" value="${esc(d.name)}"></div>
                <div class="form-group"><label class="form-label">Minimum processed frames/s (0 = no alarm)</label><input type="number" class="form-input" id="plMinFps" min="0" max="240" step="1" value="${d.min_fps || 0}"></div>
                <div class="form-group"><label class="form-label">Yield target (%)</label><input type="number" class="form-input" id="plYieldTarget" min="0" max="100" step="0.1" value="${d.yield_target || 0}">
                    <div class="pl-note">The dashboard marks the yield as below target under this value. 0 = no target.</div></div>
            </div>
            <div class="pl-form-grid" style="margin-top:6px">${cameraBoxHtml(0, cams[0])}${cameraBoxHtml(1, cams[1])}</div>
            <div class="pl-form-grid" style="margin-top:14px">
                <div class="form-group"><label class="form-label">Counting camera (two vision cameras)</label><select class="form-input" id="plCounting">
                    <option value="0" ${countingIdx !== 1 ? "selected" : ""}>Camera 1</option><option value="1" ${countingIdx === 1 ? "selected" : ""}>Camera 2</option></select>
                    <div class="pl-note">Line totals come from this camera; the other counts on its own and fires only cards that name it.</div></div>
                <div class="form-group"><label class="form-label">Sync products and codes</label>
                    <label class="pl-inline" style="margin-top:6px"><input type="checkbox" id="plSync" ${d.sync && d.sync.enabled ? "checked" : ""}> Pair each counted product with its code</label>
                    <div class="pl-note">For a vision camera and a QR reader looking at the same spot, or one vision camera that also reads codes. A picture taken on a wire line crossing pairs with the product that triggered it.</div>
                    <div class="pl-note" id="plSyncForced" style="display:none"><b>On while a camera accepts or rejects by code:</b> the line waits for the code (at most the Sync window) and then gives the product one result.</div></div>
                <div class="form-group"><label class="form-label">Sync window (ms)</label><input type="number" class="form-input" id="plSyncWindow" min="50" max="10000" step="50" value="${(d.sync && d.sync.window_ms) || 500}">
                    <div class="pl-note">Products must pass further apart than this. A reject card's travel delay must be longer.</div></div>
            </div>
            <div class="pl-note" style="margin-bottom:10px">To send code reads or results to another system, add a card under Send results below (trigger: Code read, Known code, Unknown code or No code read).</div>
            ${(d.warnings || []).map((w) => `<div class="pl-warn" style="margin:0 0 12px">${esc(w)}</div>`).join("")}
            <div class="pl-inline">
                <button class="btn-action btn-blue restricted-l2" id="plSaveSetup" ${readOnly ? "disabled" : ""}>Save line setup</button>
                ${d.enabled
                    ? `<button class="btn-action btn-outline restricted-l2" id="plStopLine" ${readOnly ? "disabled" : ""}>Stop line</button>`
                    : `<button class="btn-action btn-blue restricted-l2" id="plStartLine" ${readOnly ? "disabled" : ""}>Start line</button>`}
            </div>`;
        // A choice in one camera box can change what the other box and Sync show.
        card.querySelectorAll(".pl-cam-box select").forEach((sel) => sel.addEventListener("change", syncLineSetup));
        card.querySelectorAll(".pl-cam-box").forEach((box) => {
            const index = Number(box.dataset.index);
            renderClassLists(index);
            renderModelState(index);
            byId(`plCamModel${index}`).addEventListener("change", () => onCameraModelChange(index));
            box.addEventListener("change", (e) => {
                const input = e.target;
                if (!input.matches || !input.matches('.pl-class input[type="checkbox"]')) return;
                const own = input.closest("[data-kind]");
                // A class is a product or a defect, not both: ticking it here unticks it in the other list.
                if (input.checked) {
                    box.querySelectorAll("[data-kind]").forEach((other) => {
                        if (other === own) return;
                        other.querySelectorAll('input[type="checkbox"]').forEach((twin) => {
                            if (twin.value.toLowerCase() === input.value.toLowerCase()) twin.checked = false;
                        });
                        classCountNote(other);
                    });
                }
                classCountNote(own);
            });
            box.addEventListener("input", (e) => {
                if (!e.target.classList || !e.target.classList.contains("pl-class-filter")) return;
                const text = e.target.value.trim().toLowerCase();
                e.target.parentNode.querySelectorAll(".pl-class").forEach((label) => {
                    label.hidden = Boolean(text) && !label.querySelector("input").value.toLowerCase().includes(text);
                });
            });
        });
        syncLineSetup();
        byId("plSaveSetup").addEventListener("click", saveLineSetup);
        byId("plStartLine")?.addEventListener("click", async () => { await lineAction("start", state.lineId); renderLineSetup(); });
        byId("plStopLine")?.addEventListener("click", async () => { await lineAction("stop", state.lineId); renderLineSetup(); });
        refreshSelects();
    }

    async function saveLineSetup() {
        const cameras = [];
        const counting = byId("plCounting").value;
        [0, 1].forEach((i) => {
            const id = byId(`plCam${i}`).value;
            if (!id) return;
            // "Vision + QR code / barcode reader" is a vision camera with read_codes on.
            const picked = byId(`plRole${i}`).value;
            const role = picked === "qr" ? "qr" : "vision";
            const codes = picked !== "vision";
            const entry = { camera_id: id, role, counting: role === "vision" && String(i) === counting, qr_hold_ms: parseInt(byId(`plHold${i}`).value, 10) || 1500 };
            if (role === "vision") {
                entry.model_id = byId(`plCamModel${i}`).value || null;
                entry.expected_classes = pickedClasses(i, "expected");
                entry.defect_classes = pickedClasses(i, "defects");
            }
            if (role === "vision" && codes) entry.read_codes = true;
            if (codes) {
                entry.qr_trigger = byId(`plTrig${i}`).value || "continuous";
                entry.qr_trigger_delay_ms = Math.max(0, parseInt(byId(`plTrigDelay${i}`).value, 10) || 0);
                entry.code_type = byId(`plCodeType${i}`).value || "all";
                entry.qr_action = byId(`plQrAction${i}`).value || "report";
                entry.product_list_id = byId(`plQrList${i}`).value || null;
                entry.qr_no_read = byId(`plQrNoRead${i}`).value || "ignore";
            }
            cameras.push(entry);
        });
        const noModel = cameras.find((c) => c.role === "vision" && !c.model_id);
        if (noModel) {
            toast(`Choose the vision model for ${cameraName(noModel.camera_id)}: a vision camera cannot be saved without one`, "warning");
            return;
        }
        const noList = cameras.findIndex((c) => c.qr_action && c.qr_action !== "report" && !c.product_list_id);
        if (noList >= 0) {
            toast(`Choose the product list for ${cameraName(cameras[noList].camera_id)}: its codes are checked against it`, "warning");
            return;
        }
        // A single vision camera is always the counting camera.
        const vision = cameras.filter((c) => c.role === "vision");
        if (vision.length && !vision.some((c) => c.counting)) vision[0].counting = true;
        const body = {
            name: byId("plName").value.trim(),
            min_fps: parseFloat(byId("plMinFps").value) || 0,
            yield_target: parseFloat(byId("plYieldTarget").value) || 0,
            cameras,
            sync: { enabled: byId("plSync").checked, window_ms: parseInt(byId("plSyncWindow").value, 10) || 500 },
        };
        try {
            const saved = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}`, { method: "PUT", body });
            toast(saved.warning || "Line setup saved", saved.warning ? "warning" : "success");
            await loadLines();
            renderLineSetup();
            if (typeof checkActiveStream === "function") checkActiveStream();
        } catch (e) { toast(`Could not save the line setup: ${e.message}`, "warning"); }
    }

    // ── Line Dashboard: the line's feeds and QR reads ─────────────────────────

    // Video is fetched only while it is on screen: the Line Dashboard is open
    // and the browser tab is visible.
    const dashboardShown = () => isTab("tabDashboard") && !document.hidden;

    // The camera the main feed shows: the line's counting camera, else its
    // first vision camera, else its first camera.
    function lineFeedCamera() {
        const cams = (state.detail && state.detail.status && state.detail.status.cameras) || [];
        return cams.find((c) => c.role === "vision" && c.counting) || cams.find((c) => c.role === "vision") || cams[0] || null;
    }

    function pauseMainFeed() {
        if (typeof stopLiveFeedStream === "function") stopLiveFeedStream();
        try { _streamPaused = true; } catch (_) { /* dashboard without pause support */ }
    }

    // ── A second MJPEG feed (the dashboard's own reader drives the main one) ──

    const feeds = {};

    function stopFeed(key) {
        const feed = feeds[key];
        if (!feed) return;
        delete feeds[key];
        feed.controller.abort();
        feed.urls.forEach((u) => URL.revokeObjectURL(u));
    }

    function findHeaderEnd(bytes) {
        for (let i = 0; i <= bytes.length - 4; i++) {
            if (bytes[i] === 13 && bytes[i + 1] === 10 && bytes[i + 2] === 13 && bytes[i + 3] === 10) return i;
        }
        return -1;
    }

    async function openFeed(key, url, img, overlay) {
        stopFeed(key);
        const feed = { controller: new AbortController(), urls: new Set() };
        feeds[key] = feed;
        try {
            const res = await fetch(url, { headers: { Accept: "multipart/x-mixed-replace" }, cache: "no-store", signal: feed.controller.signal });
            if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
            const reader = res.body.getReader();
            let buf = new Uint8Array(0);
            while (feeds[key] === feed) {
                const { value, done } = await reader.read();
                if (done) break;
                if (!value || !value.length) continue;
                const joined = new Uint8Array(buf.length + value.length);
                joined.set(buf);
                joined.set(value, buf.length);
                buf = joined;
                for (;;) {
                    const headerEnd = findHeaderEnd(buf);
                    if (headerEnd < 0) { if (buf.length > 8192) throw new Error("Invalid stream"); break; }
                    const match = /Content-Length:\s*(\d+)/i.exec(new TextDecoder("ascii").decode(buf.subarray(0, headerEnd)));
                    if (!match) throw new Error("Frame without length");
                    const start = headerEnd + 4;
                    const end = start + Number(match[1]);
                    if (buf.length < end + 2) break;
                    const frameUrl = URL.createObjectURL(new Blob([buf.subarray(start, end)], { type: "image/jpeg" }));
                    const previous = img.dataset.blob;
                    img.src = frameUrl;
                    img.dataset.blob = frameUrl;
                    feed.urls.add(frameUrl);
                    if (previous && previous !== frameUrl) { URL.revokeObjectURL(previous); feed.urls.delete(previous); }
                    if (overlay) overlay.classList.remove("active");
                    buf = buf.subarray(end + 2);
                }
            }
        } catch (e) {
            if (e.name !== "AbortError" && overlay) overlay.classList.add("active");
        } finally {
            // Ended by the server or a network fault: ensureSecondFeed() opens it again.
            if (feeds[key] === feed) delete feeds[key];
        }
    }

    // ── The feed row: main feed and the line's second camera, same size ───────

    // Moves the dashboard's live video card into a two-column row next to the
    // second camera, or back into its grid when the line has one camera.
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

    function secondCamera() {
        const cams = (state.detail && state.detail.status && state.detail.status.cameras) || [];
        const main = lineFeedCamera();
        return main ? cams.find((c) => c.camera_id !== main.camera_id) || null : null;
    }

    const isTriggered = (cam) => Boolean(readsCodes(cam) && cam.qr_trigger && cam.qr_trigger !== "continuous");

    function secondView(cam) {
        if (!readsCodes(cam)) return "live";
        return state.secondView[cam.camera_id] || (isTriggered(cam) ? "capture" : "live");
    }

    function renderFeedRow() {
        const cam = secondCamera();
        const view = secondView(cam);
        const limited = Boolean(cam && cam.code_type && cam.code_type !== "all");
        // A single type's name comes from the server's list, asked for once when first needed.
        if (limited && !state.codeTypes.some((t) => t.value === cam.code_type) && !state.codeTypesAsked) {
            state.codeTypesAsked = true;
            loadCodeTypes().then(renderFeedRow);
        }
        const signature = JSON.stringify([state.lineId, cam && [cam.camera_id, cam.name || cameraName(cam.camera_id), cam.role, cam.connected, cam.qr_trigger, cam.code_type], view, state.codeTypes.length]);
        const row = byId("plFeedRow");
        if (row && row.dataset.signature === signature) return;
        stopFeed("second");
        if (!cam) { ensureFeedRow(false); return; }
        const holder = ensureFeedRow(true);
        if (!holder) return;
        holder.dataset.signature = signature;
        byId("plSecondCard")?.remove();
        const name = cam.name || cameraName(cam.camera_id);
        const detail = readsCodes(cam)
            ? `${roleLabel(cam)} · ${isTriggered(cam) ? `one picture per product on wire line ${cam.qr_trigger === "line1" ? 1 : 2}` : "reads codes continuously"}${limited ? ` · ${codeTypeLabel(cam.code_type)}` : ""}`
            : "Vision";
        const toggle = readsCodes(cam) ? `<div class="pl-seg" id="plSecondView">
                <button data-view="capture" class="${view === "capture" ? "active" : ""}">Last picture</button>
                <button data-view="live" class="${view === "live" ? "active" : ""}">Live</button></div>` : "";
        const testBtn = readsCodes(cam) && level() >= 2 ? `<button class="btn-action btn-outline" id="plTestCapture" style="font-size:12px;padding:6px 12px">Test picture</button>` : "";
        const card = document.createElement("div");
        card.className = "card-panel";
        card.id = "plSecondCard";
        card.innerHTML = `
            <div class="card-panel-header">
                <span class="card-panel-title"><span>${esc(name)}</span><span style="font-size:11px;color:var(--text-muted);font-weight:500">${esc(detail)}</span></span>
                <div class="pl-inline">${toggle}${testBtn}</div>
            </div>
            <div class="video-box">
                <img class="video-img" id="plSecondImg" alt="${esc(name)}">
                <div class="pl-feed-caption" id="plCaptureCaption" style="display:none"></div>
                <div class="no-video-overlay active" id="plSecondOverlay">
                    <div style="display:flex;flex-direction:column;align-items:center;gap:8px;opacity:.75">
                        <div style="font-size:15px;font-weight:700;color:#fff;letter-spacing:2px" id="plSecondOverlayTitle">${cam.connected ? (view === "capture" ? "NO PICTURE YET" : "CONNECTING") : "NO SIGNAL"}</div>
                        <div style="font-size:11px;color:rgba(255,255,255,.55)" id="plSecondOverlayNote">${cam.connected ? (view === "capture" ? "A picture appears when a product crosses the wire line" : "") : "This camera is not connected"}</div>
                    </div>
                </div>
            </div>`;
        holder.appendChild(card);
        card.querySelectorAll("#plSecondView button").forEach((b) => b.addEventListener("click", () => {
            state.secondView[cam.camera_id] = b.dataset.view;
            renderFeedRow();
        }));
        byId("plTestCapture")?.addEventListener("click", testCapture);
        state.captureShown = null;
        if (view === "capture") refreshLineExtras();
        else ensureSecondFeed(true);
    }

    function ensureSecondFeed(force) {
        const cam = secondCamera();
        if (!cam || !cam.connected || secondView(cam) !== "live" || !dashboardShown()) { stopFeed("second"); return; }
        if (feeds.second) return;
        const now = Date.now();
        if (!force && now < state.feedRetryAt) return;
        state.feedRetryAt = now + 3000;
        const img = byId("plSecondImg");
        if (!img) return;
        byId("plCaptureCaption").style.display = "none";
        openFeed("second", `/api/v1/vision/stream/camera/${encodeURIComponent(cam.camera_id)}?t=${now}`, img, byId("plSecondOverlay"));
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

    const SECOND_CAPTURE = { img: "plSecondImg", caption: "plCaptureCaption", overlay: "plSecondOverlay", shown: "captureShown", blob: "capture" };
    const INLINE_CAPTURE = { img: "plInlineImg", caption: "plInlineCaption", box: "plInlineCapture", shown: "inlineCaptureShown", blob: "inlineCapture" };

    async function showCapture(capture, target = SECOND_CAPTURE) {
        const img = byId(target.img);
        const caption = byId(target.caption);
        if (!img || !caption || !capture || capture.id === state[target.shown]) return;
        state[target.shown] = capture.id;
        if (target.box) byId(target.box).hidden = false;
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
        caption.innerHTML = parts.map((p) => `<span>${p}</span>`).join("");
        caption.style.display = "";
        if (capture.status === "camera_offline") return;
        try {
            const res = await fetch(`/api/v1/lines/${encodeURIComponent(state.lineId)}/qr/capture?id=${capture.id}`, { cache: "no-store" });
            if (!res.ok) return;
            const url = URL.createObjectURL(await res.blob());
            const previous = state.blobUrls[target.blob];
            state.blobUrls[target.blob] = url;
            img.src = url;
            if (target.overlay) byId(target.overlay)?.classList.remove("active");
            if (previous) setTimeout(() => URL.revokeObjectURL(previous), 2000);
        } catch (_) { /* the next refresh tries again */ }
    }

    async function testCapture() {
        const button = byId("plTestCapture");
        if (button) button.disabled = true;
        try {
            const capture = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}/qr/capture`, { method: "POST" });
            const cam = secondCamera();
            if (cam && readsCodes(cam) && capture.camera_id === cam.camera_id) {
                state.secondView[cam.camera_id] = "capture";
                renderFeedRow();
                await showCapture(capture);
            } else {
                await showCapture(capture, INLINE_CAPTURE);
            }
            toast(capture.codes && capture.codes.length ? `Test picture: ${capture.codes.map((c) => c.code).join(", ")}` : "Test picture: no code found", capture.codes && capture.codes.length ? "success" : "warning");
        } catch (e) {
            toast(`Test picture failed: ${e.message}`, "warning");
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
        const cams = (state.detail && state.detail.status && state.detail.status.cameras) || [];
        const hasQr = cams.some(readsCodes);
        const reader = mainCodeReader();
        // Rebuild only when the cameras change, so the reads list does not blink.
        const signature = JSON.stringify([state.lineId, hasQr, reader && reader.camera_id]);
        if (box.dataset.signature === signature) return;
        box.dataset.signature = signature;
        state.inlineCaptureShown = null;
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

    // A camera that reads codes and has no card of its own: the main (counting) camera.
    function mainCodeReader() {
        const cams = (state.detail && state.detail.status && state.detail.status.cameras) || [];
        const second = secondCamera();
        return cams.find((c) => readsCodes(c) && (!second || c.camera_id !== second.camera_id)) || null;
    }

    async function refreshLineExtras() {
        if (!dashboardShown() || !state.detail) return;
        ensureSecondFeed(false);
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
            const cam = secondCamera();
            const capture = data.last_capture;
            if (capture && cam && readsCodes(cam) && capture.camera_id === cam.camera_id && secondView(cam) === "capture") showCapture(capture);
            const reader = mainCodeReader();
            if (capture && reader && capture.camera_id === reader.camera_id) showCapture(capture, INLINE_CAPTURE);
        } catch (_) { /* next refresh retries */ }
    }

    // The main feed shows the selected line's counting camera. Only the Line
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
            stopFeed("second");
            stopTileFeeds();
            return;
        }
        if (isTab("tabDashboard")) {
            if (activeCamId) window.reloadStream();
            ensureSecondFeed(true);
            refreshLineExtras();
        } else if (isTab("tabOverview")) {
            refreshOverview();
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
            stopFeed("second");
        }
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
        reload: loadLines,
    };

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();
