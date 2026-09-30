/*
 * Production lines (version 2) for both dashboards: /dashboard and /dashboard/pro.
 *
 * Loaded after each dashboard's own script, and styled only with the classes
 * and colour variables both dashboards already define, so each keeps its own
 * look. It adds:
 *   - a line selector in the header; Line Dashboard and Line Logic follow it
 *     and each browser remembers its choice;
 *   - the Plant Overview, Lines and Products pages;
 *   - a Line setup card (cameras, roles, Sync, model) on Line Logic, a second
 *     camera feed and a list of recent QR reads on Line Dashboard;
 *   - Line and Role columns on Cameras, "Used by" on Communications, and a
 *     line filter on Audit Logs.
 *
 * The dashboards' existing code keeps calling the same endpoints. While a
 * line other than Line 1 is selected, the fetch wrapper below adds line_id
 * to the counting and PLC card calls and routes the Action Trigger settings
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
        timers: {},
        blobUrls: {},
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

    // ── Styles (layout only; colours come from each dashboard's variables) ────

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
        .pl-feeds { display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px; }
        .pl-feed-img { width:100%; aspect-ratio:4/3; object-fit:contain; background:#000; border-radius:6px; display:block; }
        .pl-code { font-family:monospace; font-size:12px; }
        .pl-inline { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
        .pl-inline .form-input { width:auto; flex:1; min-width:160px; }
        @media (max-width:700px) { .pl-line-select { margin-left:8px; } .pl-line-select span { display:none; } .pl-stats { grid-template-columns:repeat(2, 1fr); } }
    `;
    document.head.appendChild(style);

    // ── Per-line scoping of the existing dashboard requests ───────────────────

    // Browser-side caches the dashboards keep; each line gets its own copy.
    const PER_LINE_STORAGE = new Set(["plc_action_trigger_cards", "action_trigger_cfg", "action_trigger_endpoints"]);
    const storageKey = (key) => (PER_LINE_STORAGE.has(key) && state.lineId !== PRIMARY ? `${key}::${state.lineId}` : key);
    const nativeGet = Storage.prototype.getItem;
    const nativeSet = Storage.prototype.setItem;
    const nativeRemove = Storage.prototype.removeItem;
    Storage.prototype.getItem = function (key) { return nativeGet.call(this, this === window.localStorage ? storageKey(key) : key); };
    Storage.prototype.setItem = function (key, value) { return nativeSet.call(this, this === window.localStorage ? storageKey(key) : key, value); };
    Storage.prototype.removeItem = function (key) { return nativeRemove.call(this, this === window.localStorage ? storageKey(key) : key); };

    const LINE_SCOPED_PATHS = ["/api/v1/counting/", "/api/v1/plc/actions"];
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
        if (!state.lines.some((l) => l.id === state.lineId)) setLine(PRIMARY, false);
        renderSelector();
        return state.lines;
    }

    async function loadDetail() {
        state.detail = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}`);
        state.trigger = state.detail.action_trigger || {};
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

    function cameraName(id) {
        const cam = state.cameras.find((c) => c.id === id);
        return cam ? cam.name : id;
    }

    // ── Line selector ─────────────────────────────────────────────────────────

    function renderSelector() {
        const title = byId("pageTitle");
        if (!title) return;
        let wrap = byId("plLineSelectWrap");
        if (!wrap) {
            wrap = document.createElement("label");
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
            // The existing pages reload through the fetch wrapper, now for this line.
            if (typeof loadServerSettings === "function") loadServerSettings();
            if (typeof loadPlcActionCards === "function") loadPlcActionCards();
            if (typeof fetchStats === "function") fetchStats();
            if (typeof checkActiveStream === "function") checkActiveStream();
            if (isTab("tabActionTrigger")) renderLineSetup();
            if (isTab("tabDashboard")) renderLineExtras();
            toast(`Showing ${lineName(state.lineId)}`, "info");
        };
        let pending = false;
        try { pending = Boolean(_plcActionPendingChanges); } catch (_) { pending = false; }
        if (pending) {
            confirmAction("Unsaved PLC action changes", "The PLC action cards of this line have changes that are not applied. Switch lines and discard them?", go);
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

    function tileHtml(row) {
        const cams = row.cameras.length
            ? row.cameras.map((c) => `<span><span class="pl-dot" style="background:${c.connected ? "var(--success-color)" : "var(--danger-color)"}"></span>${esc(c.name || cameraName(c.camera_id))} · ${c.role === "qr" ? "QR" : "Vision"}</span>`).join("")
            : `<span>No camera assigned</span>`;
        const videoCam = state.tileVideo ? row.cameras.find((c) => c.connected) : null;
        const qr = row.has_qr
            ? `<div class="pl-row"><span>Codes read <b>${row.qr.codes_read}</b></span><span>Unknown <b style="color:var(--danger-color)">${row.qr.unknown}</b></span><span>No read <b style="color:var(--warning-color)">${row.qr.no_reads}</b></span></div>`
            : "";
        return `
            <div class="card-panel pl-tile" data-line="${esc(row.id)}" title="Open ${esc(row.name)}">
                <div class="pl-tile-head"><span class="pl-tile-name">${esc(row.name)}</span>${statePill(row.state)}</div>
                ${videoCam ? `<img class="pl-tile-video" data-cam="${esc(videoCam.camera_id)}" src="${esc(state.blobUrls[`tile:${videoCam.camera_id}`] || "")}" alt="Live video of ${esc(row.name)}">` : ""}
                <div class="pl-stats">
                    <div><div class="pl-stat-label">Total</div><div class="pl-stat-val">${row.total_inspected}</div></div>
                    <div><div class="pl-stat-label">Good</div><div class="pl-stat-val" style="color:var(--success-color)">${row.good_count}</div></div>
                    <div><div class="pl-stat-label">Rejected</div><div class="pl-stat-val" style="color:var(--danger-color)">${row.rejected_count}</div></div>
                    <div><div class="pl-stat-label">Per min</div><div class="pl-stat-val">${num(row.products_per_minute, 1)}</div></div>
                    <div><div class="pl-stat-label">Yield</div><div class="pl-stat-val">${num(row.yield_percentage, 1)}%</div></div>
                    <div><div class="pl-stat-label">Frames/s</div><div class="pl-stat-val" style="${row.min_fps && row.state === "running" && row.processed_fps < row.min_fps ? "color:var(--danger-color)" : ""}">${num(row.processed_fps, 1)}</div></div>
                </div>
                <div class="pl-row">${cams}</div>
                ${qr}
                <div class="pl-row"><span>Active alarms <b style="${row.active_alarms ? "color:var(--danger-color)" : ""}">${row.active_alarms}</b></span><span>Model: ${esc(row.model_name || "—")}</span></div>
            </div>`;
    }

    async function refreshOverview() {
        const grid = byId("plOverviewGrid");
        if (!grid || !isTab("tabOverview")) return;
        try {
            const data = await api("/api/v1/lines/overview");
            grid.innerHTML = (data.lines || []).map(tileHtml).join("") || `<div class="card-panel">No production lines.</div>`;
            grid.querySelectorAll(".pl-tile").forEach((tile) => tile.addEventListener("click", () => {
                if (tile.dataset.line !== state.lineId) switchLine(tile.dataset.line);
                switchTab("tabDashboard");
            }));
            grid.querySelectorAll(".pl-tile-video").forEach((img) => loadSnapshot(img, img.dataset.cam, "tile"));
        } catch (e) {
            grid.innerHTML = `<div class="card-panel">Could not load the lines: ${esc(e.message)}</div>`;
        }
    }

    async function loadSnapshot(img, cameraId, kind) {
        const key = `${kind}:${cameraId}`;
        try {
            const res = await fetch(`/api/v1/vision/annotated/camera/${encodeURIComponent(cameraId)}`);
            if (!res.ok) return;
            const url = URL.createObjectURL(await res.blob());
            const previous = state.blobUrls[key];
            state.blobUrls[key] = url;
            img.src = url;
            if (previous) setTimeout(() => URL.revokeObjectURL(previous), 2000);
        } catch (_) { /* camera went away; the next refresh retries */ }
    }

    function buildOverviewPage() {
        ensurePage("tabOverview", "Plant Overview", `
            <div class="card-panel">
                <div class="card-panel-header">
                    <span class="card-panel-title">All production lines</span>
                    <label class="pl-inline" style="font-size:12px;color:var(--text-muted);cursor:pointer">
                        <input type="checkbox" id="plTileVideo"> Live video on tiles
                    </label>
                </div>
                <div class="pl-note" style="margin-top:0">Click a line to open its dashboard. Live video is off by default to save network bandwidth.</div>
            </div>
            <div class="pl-grid" id="plOverviewGrid"></div>`);
        byId("plTileVideo").addEventListener("change", (e) => { state.tileVideo = e.target.checked; refreshOverview(); });
    }

    // ── Lines page ────────────────────────────────────────────────────────────

    function buildLinesPage() {
        ensurePage("tabLines", "Production Lines", `
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
                <div class="pl-note">A new line starts stopped and without cameras. Open its logic to assign cameras, then start it. Line 1 cannot be deleted.</div>
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
            const cams = (l.cameras || []).map((c) => `${esc(cameraName(c.camera_id))} (${c.role === "qr" ? "QR" : "Vision"}${c.counting ? ", counting" : ""})`).join("<br>") || "—";
            const running = l.enabled;
            return `<tr>
                <td><b>${esc(l.name)}</b><div class="pl-note pl-code">${esc(l.id)}</div></td>
                <td>${statePill(st.state)}</td>
                <td style="font-size:12px">${cams}</td>
                <td>${esc(st.model_name || "—")}${l.model_id ? "" : '<div class="pl-note">Server active model</div>'}</td>
                <td>${l.sync && l.sync.enabled ? `On (${l.sync.window_ms} ms)` : "Off"}</td>
                <td>${st.total_inspected ?? 0}</td>
                <td><div class="pl-actions">
                    <button class="btn-action btn-blue" data-act="logic" data-id="${esc(l.id)}">Logic</button>
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
        const role = cam ? cam.role : "vision";
        return `
            <div class="pl-cam-box">
                <h4>Camera ${index + 1}</h4>
                <div class="form-group"><label class="form-label">Camera</label><select class="form-input" id="plCam${index}">${options.join("")}</select></div>
                <div class="form-group"><label class="form-label">Role</label><select class="form-input" id="plRole${index}">
                    <option value="vision" ${role === "vision" ? "selected" : ""}>Vision (counting and inspection)</option>
                    <option value="qr" ${role === "qr" ? "selected" : ""}>QR code / barcode reader</option>
                </select></div>
                <div class="form-group" style="margin-bottom:0"><label class="form-label">QR hold time (ms)</label>
                    <input type="number" class="form-input" id="plHold${index}" min="100" max="60000" step="100" value="${cam && cam.qr_hold_ms ? cam.qr_hold_ms : 1500}">
                    <div class="pl-note">A code counts again only after it has been out of view this long (QR reader only).</div>
                </div>
            </div>`;
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
            await Promise.all([loadDetail(), loadCamerasList(), loadModelsList()]);
        } catch (e) {
            card.innerHTML = `<div class="card-panel-header"><span class="card-panel-title">Line setup</span></div><div class="pl-note">Could not load the line: ${esc(e.message)}</div>`;
            return;
        }
        const d = state.detail;
        const cams = d.cameras || [];
        const trig = d.action_trigger || {};
        const qrSel = (proto) => {
            const v = trig[`${proto}_qr_dispatch`] || "off";
            return ["off", "all", "known", "unknown"].map((o) => `<option value="${o}" ${v === o ? "selected" : ""}>${{ off: "Off", all: "Every read", known: "Known codes only", unknown: "Unknown codes only" }[o]}</option>`).join("");
        };
        const modelOptions = [`<option value="" ${!d.model_id ? "selected" : ""}>Server active model</option>`].concat(
            state.models.map((m) => `<option value="${esc(m.id)}" ${d.model_id === m.id ? "selected" : ""}>${esc(m.name)} ${esc(m.version || "")}</option>`));
        const countingIdx = cams.findIndex((c) => c.role === "vision" && c.counting);
        const readOnly = level() < 2;
        card.innerHTML = `
            <div class="card-panel-header">
                <span class="card-panel-title">Line setup · ${esc(d.name)}</span>
                <span>${statePill(d.status && d.status.state)}</span>
            </div>
            <div class="pl-form-grid">
                <div class="form-group"><label class="form-label">Line name</label><input type="text" class="form-input" id="plName" maxlength="64" value="${esc(d.name)}"></div>
                <div class="form-group"><label class="form-label">Vision model</label><select class="form-input" id="plModel">${modelOptions.join("")}</select>
                    <div class="pl-note">Lines that pick the same model share one loaded copy.</div></div>
                <div class="form-group"><label class="form-label">Minimum processed frames/s (0 = no alarm)</label><input type="number" class="form-input" id="plMinFps" min="0" max="240" step="1" value="${d.min_fps || 0}"></div>
                <div class="form-group"><label class="form-label">Connect cameras on server start</label>
                    <label class="switch" style="margin-top:6px"><input type="checkbox" id="plAutoConnect" ${d.auto_connect ? "checked" : ""}><span class="slider"></span></label></div>
            </div>
            <div class="pl-form-grid" style="margin-top:6px">${cameraBoxHtml(0, cams[0])}${cameraBoxHtml(1, cams[1])}</div>
            <div class="pl-form-grid" style="margin-top:14px">
                <div class="form-group"><label class="form-label">Counting camera (two vision cameras)</label><select class="form-input" id="plCounting">
                    <option value="0" ${countingIdx !== 1 ? "selected" : ""}>Camera 1</option><option value="1" ${countingIdx === 1 ? "selected" : ""}>Camera 2</option></select>
                    <div class="pl-note">Line totals come from this camera; the other counts on its own and fires only cards that name it.</div></div>
                <div class="form-group"><label class="form-label">Sync vision and QR camera</label>
                    <label class="pl-inline" style="margin-top:6px"><input type="checkbox" id="plSync" ${d.sync && d.sync.enabled ? "checked" : ""}> Pair each counted product with its code</label>
                    <div class="pl-note">For a vision camera and a QR reader looking at the same spot.</div></div>
                <div class="form-group"><label class="form-label">Sync window (ms)</label><input type="number" class="form-input" id="plSyncWindow" min="50" max="10000" step="50" value="${(d.sync && d.sync.window_ms) || 500}">
                    <div class="pl-note">Products must pass further apart than this. A reject card's travel delay must be longer.</div></div>
            </div>
            <div class="pl-form-grid" style="margin-top:6px">
                <div class="form-group"><label class="form-label">Send QR reads to MQTT</label><select class="form-input" id="plQrMqtt">${qrSel("mqtt")}</select></div>
                <div class="form-group"><label class="form-label">Send QR reads to TCP</label><select class="form-input" id="plQrTcp">${qrSel("tcp")}</select></div>
                <div class="form-group"><label class="form-label">Send QR reads to webhook</label><select class="form-input" id="plQrWebhook">${qrSel("webhook")}</select></div>
            </div>
            <div class="pl-note" style="margin-bottom:10px">QR reads go to the channels selected in the dispatch settings below.</div>
            <div class="pl-inline">
                <button class="btn-action btn-blue restricted-l2" id="plSaveSetup" ${readOnly ? "disabled" : ""}>Save line setup</button>
                ${d.enabled
                    ? `<button class="btn-action btn-outline restricted-l2" id="plStopLine" ${readOnly ? "disabled" : ""}>Stop line</button>`
                    : `<button class="btn-action btn-blue restricted-l2" id="plStartLine" ${readOnly ? "disabled" : ""}>Start line</button>`}
            </div>`;
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
            const role = byId(`plRole${i}`).value;
            cameras.push({ camera_id: id, role, counting: role === "vision" && String(i) === counting, qr_hold_ms: parseInt(byId(`plHold${i}`).value, 10) || 1500 });
        });
        // A single vision camera is always the counting camera.
        const vision = cameras.filter((c) => c.role === "vision");
        if (vision.length && !vision.some((c) => c.counting)) vision[0].counting = true;
        const trigger = {
            ...(state.detail.action_trigger || {}),
            mqtt_qr_dispatch: byId("plQrMqtt").value,
            tcp_qr_dispatch: byId("plQrTcp").value,
            webhook_qr_dispatch: byId("plQrWebhook").value,
        };
        const body = {
            name: byId("plName").value.trim(),
            model_id: byId("plModel").value || null,
            min_fps: parseFloat(byId("plMinFps").value) || 0,
            auto_connect: byId("plAutoConnect").checked,
            cameras,
            sync: { enabled: byId("plSync").checked, window_ms: parseInt(byId("plSyncWindow").value, 10) || 500 },
            action_trigger: trigger,
        };
        try {
            const saved = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}`, { method: "PUT", body });
            toast(saved.warning || "Line setup saved", saved.warning ? "warning" : "success");
            await loadLines();
            renderLineSetup();
            if (typeof checkActiveStream === "function") checkActiveStream();
        } catch (e) { toast(`Could not save the line setup: ${e.message}`, "warning"); }
    }

    // ── Line Dashboard extras: second feed and QR reads ───────────────────────

    function renderLineExtras() {
        const page = byId("tabDashboard");
        if (!page) return;
        let box = byId("plLineExtras");
        if (!box) {
            box = document.createElement("div");
            box.id = "plLineExtras";
            page.appendChild(box);
        }
        const d = state.detail;
        const cams = (d && d.status && d.status.cameras) || [];
        const main = cams.find((c) => c.role === "vision" && c.counting) || cams[0];
        const second = cams.find((c) => c !== main);
        const hasQr = cams.some((c) => c.role === "qr");
        // Rebuild only when the cameras change, so the feed and the reads list do not blink.
        const signature = JSON.stringify([state.lineId, second && [second.camera_id, second.role, second.connected], hasQr]);
        if (box.dataset.signature === signature) return;
        box.dataset.signature = signature;
        let html = "";
        if (second) {
            html += `<div class="card-panel"><div class="card-panel-header"><span class="card-panel-title">Camera 2 · ${esc(second.name || cameraName(second.camera_id))} · ${second.role === "qr" ? "QR reader" : "Vision"}</span>
                <span style="font-size:11px;color:var(--text-muted)">${second.role === "qr" ? "Known codes outlined green, unknown red" : ""}</span></div>
                ${second.connected ? `<img class="pl-feed-img" id="plSecondFeed" data-cam="${esc(second.camera_id)}" src="${esc(state.blobUrls[`feed:${second.camera_id}`] || "")}" alt="Camera 2">` : `<div class="pl-note">Camera 2 is not connected.</div>`}</div>`;
        }
        if (hasQr) {
            html += `<div class="card-panel"><div class="card-panel-header"><span class="card-panel-title">Latest code reads</span><span id="plQrStats" style="font-size:12px;color:var(--text-muted)"></span></div>
                <div class="table-responsive"><table class="pl-table"><thead><tr><th>Time</th><th>Code</th><th>Result</th><th>Product</th><th>Class</th></tr></thead>
                <tbody id="plQrBody"><tr><td colspan="5">No reads yet.</td></tr></tbody></table></div></div>`;
        }
        box.innerHTML = html;
    }

    const QR_RESULT = {
        known: ["Known", "var(--success-color)"],
        unknown: ["Unknown", "var(--danger-color)"],
        no_read: ["No read", "var(--warning-color)"],
    };

    async function refreshLineExtras() {
        if (!isTab("tabDashboard") || !state.detail) return;
        const feed = byId("plSecondFeed");
        if (feed) loadSnapshot(feed, feed.dataset.cam, "feed");
        const body = byId("plQrBody");
        if (!body) return;
        try {
            const data = await api(`/api/v1/lines/${encodeURIComponent(state.lineId)}/qr/recent?limit=25`);
            const s = data.stats || {};
            byId("plQrStats").textContent = `Read ${s.codes_read || 0} · Unknown ${s.unknown || 0} · No read ${s.no_reads || 0} · Unpaired ${s.unpaired || 0}`;
            body.innerHTML = (data.reads || []).map((r) => {
                const [label, color] = QR_RESULT[r.status] || [r.status, "var(--text-muted)"];
                const paired = r.paired === false && r.status !== "no_read" ? " (unpaired)" : "";
                return `<tr><td class="pl-code">${esc(new Date(r.timestamp).toLocaleTimeString())}</td><td class="pl-code">${esc(r.code || "—")}</td>
                    <td><b style="color:${color}">${esc(label)}</b>${esc(paired)}</td><td>${esc(r.product_name || "—")}</td><td>${esc(r.class_name || "—")}</td></tr>`;
            }).join("") || `<tr><td colspan="5">No reads yet.</td></tr>`;
        } catch (_) { /* next refresh retries */ }
    }

    // The live feed shows the selected line's counting camera (or its first camera).
    const originalCheckActiveStream = window.checkActiveStream;
    // quiet: the periodic refresh below, which reloads the stream only when the camera changed.
    window.checkActiveStream = async function (quiet) {
        let detail = null;
        try { detail = await loadDetail(); } catch (_) { detail = null; }
        if (!detail || (!(detail.cameras || []).length && state.lineId === PRIMARY)) {
            renderLineExtras();
            return originalCheckActiveStream();
        }
        const cams = (detail.status && detail.status.cameras) || [];
        const main = cams.find((c) => c.role === "vision" && c.counting) || cams[0];
        if (main && main.connected) {
            const changed = activeCamId !== main.camera_id;
            activeCamId = main.camera_id;
            if ((quiet !== true || changed) && isTab("tabDashboard")) reloadStream();
        } else {
            activeCamId = null;
            stopLiveFeedStream();
            byId("noVideoOverlay")?.classList.add("active");
        }
        renderLineExtras();
    };

    // ── Products page ─────────────────────────────────────────────────────────

    function buildProductsPage() {
        ensurePage("tabProducts", "Products", `
            <div class="card-panel">
                <div class="card-panel-header">
                    <span class="card-panel-title">Product codes</span>
                    <div class="pl-inline">
                        <button class="btn-action btn-outline" id="plExport">Export CSV</button>
                        <label class="btn-action btn-outline" style="cursor:pointer">Import CSV<input type="file" id="plImportFile" accept=".csv,text/csv" style="display:none"></label>
                        <label class="pl-inline" style="font-size:12px;color:var(--text-muted)"><input type="checkbox" id="plImportReplace"> Replace the whole list</label>
                    </div>
                </div>
                <div class="pl-note" style="margin-top:0;margin-bottom:12px">Every QR reader checks the codes it reads against this list. CSV files need a header row: <span class="pl-code">code,name,description</span>.</div>
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
        let timer = null;
        byId("plProdSearch").addEventListener("input", (e) => {
            clearTimeout(timer);
            timer = setTimeout(() => { state.productSearch = e.target.value.trim(); refreshProducts(); }, 250);
        });
    }

    async function refreshProducts() {
        const body = byId("plProdBody");
        if (!body) return;
        try {
            const q = state.productSearch ? `&search=${encodeURIComponent(state.productSearch)}` : "";
            const data = await api(`/api/v1/products?limit=500${q}`);
            const canEdit = level() >= 2;
            byId("plProdCount").textContent = `${data.total} code(s)${data.total > data.products.length ? `, showing ${data.products.length}` : ""}`;
            body.innerHTML = data.products.map((p) => `<tr data-id="${esc(p.id)}">
                <td class="pl-code">${esc(p.code)}</td><td>${esc(p.name)}</td><td style="font-size:12px;color:var(--text-muted)">${esc(p.description || "")}</td>
                <td><div class="pl-actions">${canEdit ? `<button class="btn-action btn-outline" data-act="edit">Edit</button><button class="btn-action btn-red" data-act="delete">Delete</button>` : ""}</div></td>
            </tr>`).join("") || `<tr><td colspan="4">No product codes yet.</td></tr>`;
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
            await api("/api/v1/products", { method: "POST", body: { code, name, description } });
            ["plProdCode", "plProdName", "plProdDesc"].forEach((id) => { byId(id).value = ""; });
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
        confirmAction("Delete product code", `Delete ${p.code} (${p.name})? QR readers will report it as unknown.`, async () => {
            try {
                await api(`/api/v1/products/${encodeURIComponent(p.id)}`, { method: "DELETE" });
                toast("Product deleted", "success");
                refreshProducts();
            } catch (e) { toast(`Could not delete the product: ${e.message}`, "warning"); }
        });
    }

    async function exportProducts() {
        try {
            const res = await fetch("/api/v1/products/export");
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const url = URL.createObjectURL(await res.blob());
            const a = document.createElement("a");
            a.href = url;
            a.download = "products.csv";
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) { toast(`Could not export: ${e.message}`, "warning"); }
    }

    async function importProducts(e) {
        const file = e.target.files && e.target.files[0];
        e.target.value = "";
        if (!file) return;
        const replace = byId("plImportReplace").checked;
        const run = async () => {
            const form = new FormData();
            form.append("file", file);
            try {
                const r = await api(`/api/v1/products/import?replace=${replace}`, { method: "POST", body: form });
                toast(`Imported: ${r.added} added, ${r.updated} updated, ${r.removed} removed (${r.total} total)`, "success");
                refreshProducts();
            } catch (err) { toast(`Import failed: ${err.message}`, "warning"); }
        };
        if (replace) confirmAction("Replace product list", "Codes that are not in the file will be deleted. Continue?", run);
        else run();
    }

    // ── Cameras: Line and Role columns ────────────────────────────────────────

    const originalLoadCameras = window.loadCameras;
    window.loadCameras = async function () {
        const result = await originalLoadCameras.apply(this, arguments);
        try {
            if (!state.lines.length) await loadLines();
            const owner = {};
            state.lines.forEach((l) => (l.cameras || []).forEach((c) => { owner[c.camera_id] = { line: l.name, role: c.role }; }));
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
                    <td class="pl-td" style="font-size:12px">${o ? (o.role === "qr" ? "QR reader" : "Vision") : ""}</td>`);
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
            panelHeader.insertAdjacentHTML("beforeend", `<label class="pl-inline" style="font-size:12px;color:var(--text-muted)">Line
                <select id="plAuditLine" class="form-input" style="min-width:150px"></select></label>`);
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

    function relabelMenu(tab, label) {
        document.querySelectorAll(".menu-item").forEach((m) => {
            if ((m.getAttribute("onclick") || "").includes(`'${tab}'`)) {
                const spans = m.querySelectorAll("span");
                const text = spans[spans.length - 1];
                if (text && !text.classList.contains("menu-icon")) text.textContent = label;
            }
        });
    }

    function buildMenu() {
        const dashItem = [...document.querySelectorAll(".menu-item")].find((m) => (m.getAttribute("onclick") || "").includes("'tabDashboard'"));
        const logicItem = [...document.querySelectorAll(".menu-item")].find((m) => (m.getAttribute("onclick") || "").includes("'tabActionTrigger'"));
        if (!dashItem || byId("plMenu_tabOverview")) return;
        dashItem.parentNode.insertBefore(menuItem("tabOverview", "Plant Overview"), dashItem);
        const after = logicItem || dashItem;
        const lines = menuItem("tabLines", "Lines");
        const products = menuItem("tabProducts", "Products");
        after.insertAdjacentElement("afterend", lines);
        lines.insertAdjacentElement("afterend", products);
        relabelMenu("tabDashboard", "Line Dashboard");
        relabelMenu("tabActionTrigger", "Line Logic");
        TAB_TITLES.tabDashboard = "Line Dashboard";
        TAB_TITLES.tabActionTrigger = "Line Logic";
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
        stopTimers();
        applyRoleVisibility();
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
            loadDetail().then(() => { renderLineExtras(); refreshLineExtras(); }).catch(() => {});
            state.timers.extras = setInterval(refreshLineExtras, 1000);
            state.timers.lineCams = setInterval(() => window.checkActiveStream(true), 5000);
        }
        return result;
    };

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
        }).catch(() => { /* not signed in yet; retried after login */ });
    }

    // Re-read lines after sign-in (the dashboard calls initDashboard then).
    const originalInitDashboard = window.initDashboard;
    if (typeof originalInitDashboard === "function") {
        window.initDashboard = function () {
            const result = originalInitDashboard.apply(this, arguments);
            applyRoleVisibility();
            loadLines().then(() => { if (typeof checkActiveStream === "function") checkActiveStream(); }).catch(() => {});
            return result;
        };
    }

    window.ProductionLines = {
        get lineId() { return state.lineId; },
        lineCameras: () => ((state.detail && state.detail.cameras) || []).map((c) => ({ ...c, name: cameraName(c.camera_id) })),
        reload: loadLines,
    };

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();
