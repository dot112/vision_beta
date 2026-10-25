/*
 * System usage in the top bar: a CPU ring and a GPU ring with the usage in percent inside,
 * and, on a click, a panel with the CPU, the GPUs, the RAM, the disks and this server's share.
 *
 * It asks GET /api/v1/system/usage every 2 s while signed in and the page is visible (one
 * request at a time), and stops on a 401 (dashboard.html starts it again after a sign-in).
 * The panel is the account menu's dropdown (toggleMenu / closeMenus in dashboard.html).
 * Runs in node for tests/test_system_usage_dashboard.py (module.exports).
 */
(function (root) {
    "use strict";

    const POLL_MS = 2000;
    const STALE_MS = 10000;      // the rings grey after this long without an answer
    const WARN = 70;
    const HIGH = 90;
    const RING = 2 * Math.PI * 13;  // the rings' circle: r = 13 in a 30 x 30 box

    const esc = (value) => String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));

    // The colour follows the whole percent shown: a ring that says "90%" is red.
    function level(percent) {
        if (!known(percent)) return "none";
        const shown = Math.round(percent);
        return shown >= HIGH ? "high" : shown >= WARN ? "warn" : "ok";
    }

    const ringText = (percent) => (known(percent) ? `${Math.round(percent)}%` : "—");
    // "100%" needs a smaller font to fit inside the ring.
    const wideText = (percent) => known(percent) && Math.round(percent) >= 100;
    const isStale = (lastAnswerMs, nowMs) => nowMs - lastAnswerMs > STALE_MS;

    function busiestGpu(gpus) {
        return (gpus || []).reduce((best, gpu) => (gpu && (!best || (gpu.percent || 0) > (best.percent || 0)) ? gpu : best), null);
    }

    function formatBytes(bytes) {
        if (!known(bytes)) return "—";
        const gb = bytes / 1024 ** 3;
        return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    }

    // ── The panel ─────────────────────────────────────────────────────────────

    const bar = (percent, extra = "") =>
        `<span class="usage-bar usage-${level(percent)}${extra}"><span style="width:${known(percent) ? Math.max(0, Math.min(100, percent)) : 0}%"></span></span>`;
    const head = (title, percent) => `<div class="usage-sec-head"><b>${title}</b><span>${ringText(percent)}</span></div>`;
    const facts = (items) => `<div class="usage-facts">${items.filter(Boolean).map((i) => `<span>${i}</span>`).join("")}</div>`;
    const ghz = (mhz) => `${(mhz / 1000).toFixed(1)} GHz`;

    function panelHtml(reading) {
        const r = reading || {};
        const cpu = r.cpu || {};
        const ram = r.memory || {};
        const server = r.server || {};
        // One upright bar per core: its fill grows upwards.
        const cores = (cpu.per_core || []).map((p) => `<span class="usage-core" title="${ringText(p)}"><span class="usage-bar usage-bar-v usage-${level(p)}"><span style="height:${known(p) ? Math.max(0, Math.min(100, p)) : 0}%"></span></span></span>`).join("");
        const gpus = (r.gpus || []).map((g) => `
            <div class="usage-item">
                <div class="usage-item-head"><span>${esc(g.name)}</span><span>${ringText(g.percent)}</span></div>${bar(g.percent)}
                ${facts([known(g.memory_total) ? `Memory ${formatBytes(g.memory_used)} of ${formatBytes(g.memory_total)}` : "",
                         known(g.temp_c) ? `${Math.round(g.temp_c)} °C` : ""])}</div>`).join("");
        const disks = (r.disks || []).map((d) => `
            <div class="usage-item">
                <div class="usage-item-head"><span>${esc(d.mount)}${d.server_data ? ' <em class="usage-tag">server data</em>' : ""}</span><span>${ringText(d.percent)}</span></div>${bar(d.percent)}
                ${facts([`${formatBytes(d.used)} of ${formatBytes(d.total)}`, `${formatBytes(d.free)} free`])}</div>`).join("");
        return `
            <div class="usage-sec">${head("CPU", cpu.percent)}${bar(cpu.percent)}
                <div class="usage-cores">${cores}</div>
                ${facts([known(cpu.cores_logical) ? `${esc(cpu.cores_logical)} logical / ${esc(cpu.cores_physical || "?")} physical cores` : "",
                         known(cpu.freq_mhz) ? `${ghz(cpu.freq_mhz)}${known(cpu.freq_max_mhz) ? ` of ${ghz(cpu.freq_max_mhz)}` : ""}` : "",
                         Array.isArray(cpu.load_avg) ? `Load ${cpu.load_avg.map((v) => v.toFixed(1)).join(" ")}` : "",
                         known(cpu.temp_c) ? `${Math.round(cpu.temp_c)} °C` : ""])}</div>
            <div class="usage-sec">${head("GPU", (busiestGpu(r.gpus) || {}).percent)}
                ${gpus || '<div class="usage-empty">No GPU reading on this machine.</div>'}</div>
            <div class="usage-sec">${head("RAM", ram.percent)}${bar(ram.percent)}
                ${facts([`${formatBytes(ram.used)} of ${formatBytes(ram.total)}`,
                         ram.swap_total ? `Swap ${formatBytes(ram.swap_used)} of ${formatBytes(ram.swap_total)}` : ""])}</div>
            <div class="usage-sec">${head("Storage", null)}${disks || '<div class="usage-empty">No disk reading.</div>'}</div>
            <div class="usage-sec">${head("This server", server.cpu_percent)}
                ${facts([`CPU ${ringText(server.cpu_percent)}`, `RAM ${formatBytes(server.memory_rss)}${known(server.memory_percent) ? ` (${server.memory_percent}%)` : ""}`])}</div>`;
    }

    // ── The rings ─────────────────────────────────────────────────────────────

    const byId = (id) => (root.document && root.document.getElementById ? root.document.getElementById(id) : null);

    function setRing(button, percent) {
        if (!button) return;
        button.classList.remove("usage-ok", "usage-warn", "usage-high", "usage-none");
        button.classList.add(`usage-${level(percent)}`);
        const fg = button.querySelector(".usage-ring-fg");
        if (fg) fg.setAttribute("stroke-dasharray", `${known(percent) ? (RING * Math.max(0, Math.min(100, percent))) / 100 : 0} ${RING}`);
        const value = button.querySelector(".usage-ring-val");
        if (value) {
            value.textContent = ringText(percent);
            value.classList.toggle("wide", wideText(percent));
        }
    }

    const state = { running: false, timer: null, reading: null, lastAnswer: 0 };

    function render() {
        const menu = byId("usageMenu");
        if (!menu) return;
        const r = state.reading || {};
        setRing(menu.querySelector('[data-usage="cpu"]'), (r.cpu || {}).percent);
        setRing(menu.querySelector('[data-usage="gpu"]'), (busiestGpu(r.gpus) || {}).percent);
        menu.classList.toggle("stale", isStale(state.lastAnswer, Date.now()));
        const panel = byId("usagePanel");
        if (panel && menu.classList.contains("open")) panel.innerHTML = panelHtml(state.reading);
    }

    function schedule() {
        clearTimeout(state.timer);
        state.timer = state.running ? setTimeout(tick, POLL_MS) : null;
    }

    async function tick() {
        state.timer = null;
        if (!state.running) return;
        const signedIn = root.sessionStorage && root.sessionStorage.getItem("access_token");
        if (!signedIn || (root.document && root.document.hidden)) { schedule(); return; }
        try {
            const res = await root.fetch("/api/v1/system/usage", { cache: "no-store" });
            if (res.status === 401) { stop(); return; }
            if (res.ok) {
                state.reading = await res.json();
                state.lastAnswer = Date.now();
            }
        } catch (_) { /* the next tick asks again; the rings grey after 10 s */ }
        render();
        schedule();
    }

    function start() {
        if (state.running) return;
        state.running = true;
        if (!state.lastAnswer) state.lastAnswer = Date.now();
        const menu = byId("usageMenu");
        if (menu && !menu.dataset.bound) {
            menu.dataset.bound = "1";
            // toggleMenu (the buttons' onclick) opens the panel first; fill it at once.
            menu.addEventListener("click", () => setTimeout(render, 0));
        }
        tick();
    }

    function stop() {
        state.running = false;
        clearTimeout(state.timer);
        state.timer = null;
    }

    const api = { level, ringText, wideText, busiestGpu, formatBytes, isStale, panelHtml, start, stop };
    root.SystemUsage = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
