/*
 * Image processing of a camera that reads codes: Line setup's "Image processing" fold.
 *
 * foldBody(cam) is the fold's markup, collect(card) the settings it saves with the camera
 * (code_processing, checked again by the server), and open(card) starts its live preview:
 * the camera's picture read with the steps as they are set now (not saved yet), from
 * POST /api/v1/qr/preview/camera/{id}, one request at a time while the fold is open, the card
 * is on the page, Line setup is shown and the browser tab is visible. On the Original view,
 * with Perspective on, the four corners are dragged on the picture.
 *
 * production_lines.js adds the fold to each code reader card and calls these.
 * Runs in node for tests/test_code_processing_dashboard.py (module.exports).
 */
(function (root) {
    "use strict";

    // As the server's DEFAULT_PROCESSING (app/engines/code_processing.py).
    const DEFAULTS = {
        enabled: false,
        perspective: { on: false, corners: [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]] },
        rotate: { on: false, angle: 0 },
        scale: { on: false, factor: 1 },
        grayscale: { on: false },
        denoise: { on: false, strength: 2 },
        contrast: { on: false, strength: 3 },
        sharpen: { on: false, strength: 3 },
        binarize: { on: false, method: "auto", level: 128, block: 31, invert: false },
        crop_margin_px: 16,
    };
    const PREVIEW_GAP_MS = 250;     // the next picture at least this long after the last answer
    const RETRY_MS = 2000;          // after "not connected" or an error
    const IDLE_MS = 1000;           // while the fold is open but not seen (another page, a hidden tab)
    const HANDLE_PX = 14;           // how near a corner a press picks it up
    const READ_BY = {
        processed: "read in the processed picture",
        straightened: "read straightened",
        original: "read in the original picture",
    };
    const HELP_SWITCH = "Processes a copy of the picture before codes are read: the video keeps showing the camera's own picture.";
    const HELP_CORNERS = "Drag the four corners around the label on the Original view: that area is made flat and straight before reading.";

    const esc = (value) => String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    const copy = (value) => JSON.parse(JSON.stringify(value));
    const field = (card, name) => card.querySelector(`[data-f="${name}"]`);
    const ticked = (card, name) => Boolean(field(card, name) && field(card, name).checked);
    function number(card, name, fallback) {
        const input = field(card, name);
        const value = input ? parseFloat(input.value) : NaN;
        return Number.isFinite(value) ? value : fallback;
    }

    // The saved settings with the defaults under them, for showing in the fold.
    function merged(saved) {
        const out = copy(DEFAULTS);
        if (!saved || typeof saved !== "object") return out;
        Object.keys(out).forEach((key) => {
            if (saved[key] === undefined) return;
            out[key] = out[key] && typeof out[key] === "object" && !Array.isArray(out[key]) ? { ...out[key], ...saved[key] } : saved[key];
        });
        return out;
    }

    function cornersOf(card) {
        try {
            const value = JSON.parse(field(card, "proc_corners").value);
            if (Array.isArray(value) && value.length === 4 && value.every((p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite))) return value;
        } catch (_) { /* not set yet */ }
        return copy(DEFAULTS.perspective.corners);
    }

    // ── The settings the fold holds ───────────────────────────────────────────

    function collect(card) {
        const d = DEFAULTS;
        const whole = (name, fallback) => Math.round(number(card, name, fallback));
        const methodField = field(card, "proc_bin_method");
        return {
            enabled: ticked(card, "proc_enabled"),
            perspective: { on: ticked(card, "proc_persp"), corners: cornersOf(card) },
            rotate: { on: ticked(card, "proc_rotate"), angle: number(card, "proc_angle", d.rotate.angle) },
            scale: { on: ticked(card, "proc_scale"), factor: number(card, "proc_factor", d.scale.factor) },
            grayscale: { on: ticked(card, "proc_gray") },
            denoise: { on: ticked(card, "proc_denoise"), strength: whole("proc_denoise_n", d.denoise.strength) },
            contrast: { on: ticked(card, "proc_contrast"), strength: whole("proc_contrast_n", d.contrast.strength) },
            sharpen: { on: ticked(card, "proc_sharpen"), strength: whole("proc_sharpen_n", d.sharpen.strength) },
            binarize: {
                on: ticked(card, "proc_bin"),
                method: (methodField && methodField.value) || d.binarize.method,
                level: whole("proc_bin_level", d.binarize.level),
                block: whole("proc_bin_block", d.binarize.block),
                invert: ticked(card, "proc_invert"),
            },
            crop_margin_px: whole("proc_margin", d.crop_margin_px),
        };
    }

    function statusText(codes, readBy, ms) {
        const time = `${Math.round(Number(ms) || 0)} ms`;
        if (!codes) return `No code read · ${time}`;
        return `${codes} code${codes === 1 ? "" : "s"} · ${READ_BY[readBy] || readBy} · ${time}`;
    }

    // ── Markup ────────────────────────────────────────────────────────────────

    function foldBody(cam) {
        const s = merged(cam && cam.code_processing);
        const check = (name, on, label, help) =>
            `<label class="pl-inline pl-check"${help ? ` data-help="${help}"` : ""}><input type="checkbox" data-f="${name}" ${on ? "checked" : ""}> ${label}</label>`;
        const num = (name, value, min, max, step, label) =>
            `<label class="pl-proc-num">${label}<input type="number" class="form-input" data-f="${name}" min="${min}" max="${max}" step="${step}" value="${esc(value)}"></label>`;
        const method = (value, label) => `<option value="${value}" ${s.binarize.method === value ? "selected" : ""}>${label}</option>`;
        return `
            <div class="form-group">${check("proc_enabled", s.enabled, "Process the picture before reading", HELP_SWITCH)}
                <div class="pl-note">The video keeps showing the camera's own picture; only the reading uses the processed one. When the processed picture reads nothing, the original is read too.</div></div>
            <div class="pl-proc-steps">
                <div class="pl-proc-row">${check("proc_persp", s.perspective.on, "Perspective", HELP_CORNERS)}
                    <span class="pl-note">Drag the four corners on the Original view.</span>
                    <button data-lock type="button" class="pl-link" data-act="proc-reset-corners">Reset corners</button>
                    <input type="hidden" data-f="proc_corners" value="${esc(JSON.stringify(s.perspective.corners))}"></div>
                <div class="pl-proc-row">${check("proc_rotate", s.rotate.on, "Rotate")}${num("proc_angle", s.rotate.angle, -180, 180, 0.5, "Angle (°, clockwise)")}</div>
                <div class="pl-proc-row">${check("proc_scale", s.scale.on, "Scale")}${num("proc_factor", s.scale.factor, 0.25, 4, 0.25, "Factor (×)")}</div>
                <div class="pl-proc-row">${check("proc_gray", s.grayscale.on, "Grayscale")}</div>
                <div class="pl-proc-row">${check("proc_denoise", s.denoise.on, "Denoise")}${num("proc_denoise_n", s.denoise.strength, 1, 5, 1, "Strength (1–5)")}</div>
                <div class="pl-proc-row">${check("proc_contrast", s.contrast.on, "Contrast")}${num("proc_contrast_n", s.contrast.strength, 1, 10, 1, "Strength (1–10)")}</div>
                <div class="pl-proc-row">${check("proc_sharpen", s.sharpen.on, "Sharpen")}${num("proc_sharpen_n", s.sharpen.strength, 1, 10, 1, "Strength (1–10)")}</div>
                <div class="pl-proc-row">${check("proc_bin", s.binarize.on, "Binarize")}
                    <select class="form-input pl-proc-method" data-f="proc_bin_method">${method("auto", "Automatic")}${method("adaptive", "Adaptive (uneven light)")}${method("fixed", "Fixed level")}</select>
                    <span data-proc-for="fixed">${num("proc_bin_level", s.binarize.level, 0, 255, 1, "Level (0–255)")}</span>
                    <span data-proc-for="adaptive">${num("proc_bin_block", s.binarize.block, 3, 255, 2, "Block size (px)")}</span>
                    ${check("proc_invert", s.binarize.invert, "Invert")}</div>
                <div class="pl-proc-row">${num("proc_margin", s.crop_margin_px, 0, 200, 1, "Crop margin (px)")}</div>
            </div>
            <div class="seg pl-proc-views" role="group" aria-label="Preview">
                <button type="button" data-act="proc-view" data-view="processed" aria-pressed="true">Processed</button>
                <button type="button" data-act="proc-view" data-view="original" aria-pressed="false">Original</button>
            </div>
            <div class="pl-lines-pic pl-proc-pic" data-f="proc-pic"><img alt="" hidden><canvas></canvas></div>
            <div class="pl-note" data-f="proc-status">The preview starts when this section is open.</div>`;
    }

    // ── Live preview ──────────────────────────────────────────────────────────

    const states = new WeakMap();   // card -> its preview's state
    function stateOf(card) {
        let st = states.get(card);
        if (!st) {
            st = { view: "processed", timer: null, busy: false, bound: false, drag: null, url: null };
            states.set(card, st);
        }
        return st;
    }

    const foldOf = (card) => card.querySelector('.pl-fold[data-fold="processing"]');
    const foldOpen = (card) => Boolean(card.isConnected && foldOf(card) && foldOf(card).classList.contains("open"));
    const seen = (card) => foldOpen(card) && !foldOf(card).hidden && root.document.body.dataset.tab === "tabActionTrigger" && !root.document.hidden;
    const locked = () => Boolean(root.ConfigLock && root.ConfigLock.locked && root.ConfigLock.locked());

    function setStatus(card, text) {
        const status = field(card, "proc-status");
        if (status) status.textContent = text;
    }

    function schedule(card, wait) {
        const st = stateOf(card);
        clearTimeout(st.timer);
        st.timer = foldOpen(card) ? setTimeout(() => tick(card), wait) : null;
    }

    async function tick(card) {
        const st = stateOf(card);
        st.timer = null;
        if (st.busy) return;
        if (!seen(card)) { schedule(card, IDLE_MS); return; }
        st.busy = true;
        let wait = PREVIEW_GAP_MS;
        try {
            const codeType = field(card, "code_type");
            const res = await root.fetch(`/api/v1/qr/preview/camera/${encodeURIComponent(card.dataset.camera)}`, {
                method: "POST",
                cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ processing: collect(card), code_type: (codeType && codeType.value) || "all", view: st.view }),
            });
            if (res.ok) {
                showPicture(card, await res.blob());
                setStatus(card, statusText(parseInt(res.headers.get("X-Codes") || "0", 10), res.headers.get("X-Read-By"), res.headers.get("X-Read-Ms")));
            } else if (res.status === 409) {
                setStatus(card, "Camera not connected");
                wait = RETRY_MS;
            } else {
                let detail = "";
                try { detail = (await res.json()).detail; } catch (_) { /* no JSON */ }
                setStatus(card, typeof detail === "string" && detail ? detail : `The preview failed (${res.status})`);
                wait = RETRY_MS;
            }
        } catch (_) {
            setStatus(card, "No answer from the server");
            wait = RETRY_MS;
        }
        st.busy = false;
        schedule(card, wait);
    }

    function showPicture(card, blob) {
        const st = stateOf(card);
        const box = field(card, "proc-pic");
        if (!box) return;
        const img = box.querySelector("img");
        const url = URL.createObjectURL(blob);
        const previous = st.url;
        st.url = url;
        img.onload = () => {
            box.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
            img.hidden = false;
            if (previous) URL.revokeObjectURL(previous);
            drawCorners(card);
        };
        img.src = url;
    }

    // The four corners and their outline, on the Original view while Perspective is on.
    function drawCorners(card) {
        const box = field(card, "proc-pic");
        if (!box) return;
        const canvas = box.querySelector("canvas");
        const w = box.clientWidth;
        const h = box.clientHeight;
        const ratio = root.devicePixelRatio || 1;
        canvas.width = Math.round(w * ratio);
        canvas.height = Math.round(h * ratio);
        const ctx = canvas.getContext("2d");
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, w, h);
        const active = stateOf(card).view === "original" && ticked(card, "proc_persp");
        box.classList.toggle("can-drag", active && !locked());
        if (!active || !w || !h) return;
        const points = cornersOf(card).map(([x, y]) => [x * w, y * h]);
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#3b82f6";
        ctx.beginPath();
        points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.closePath();
        ctx.stroke();
        points.forEach(([x, y]) => {
            ctx.beginPath();
            ctx.arc(x, y, 6, 0, Math.PI * 2);
            ctx.fillStyle = "#ffffff";
            ctx.fill();
            ctx.stroke();
        });
    }

    function setCorners(card, corners) {
        const hidden = field(card, "proc_corners");
        hidden.value = JSON.stringify(corners.map(([x, y]) => [Math.round(x * 1000) / 1000, Math.round(y * 1000) / 1000]));
        hidden.dispatchEvent(new Event("input", { bubbles: true }));   // marks Line setup unsaved
        drawCorners(card);
    }

    function syncFields(card) {
        const method = (field(card, "proc_bin_method") || {}).value || "auto";
        card.querySelectorAll("[data-proc-for]").forEach((part) => { part.hidden = part.dataset.procFor !== method; });
        drawCorners(card);
    }

    function bind(card) {
        const st = stateOf(card);
        if (st.bound) return;
        st.bound = true;
        const fold = foldOf(card);
        fold.addEventListener("change", () => syncFields(card));
        fold.addEventListener("click", (e) => {
            const button = e.target.closest("[data-act]");
            if (!button || !fold.contains(button)) return;
            if (button.dataset.act === "proc-view") {
                st.view = button.dataset.view;
                fold.querySelectorAll('[data-act="proc-view"]').forEach((b) => b.setAttribute("aria-pressed", b === button ? "true" : "false"));
                drawCorners(card);
                if (!st.busy) tick(card);
            } else if (button.dataset.act === "proc-reset-corners" && !locked()) {
                setCorners(card, copy(DEFAULTS.perspective.corners));
            }
        });
        const canvas = field(card, "proc-pic").querySelector("canvas");
        const at = (e) => {
            const r = canvas.getBoundingClientRect();
            return [Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1), Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1), r];
        };
        canvas.addEventListener("pointerdown", (e) => {
            if (st.view !== "original" || !ticked(card, "proc_persp") || locked()) return;
            const [x, y, r] = at(e);
            let best = null;
            cornersOf(card).forEach(([cx, cy], i) => {
                const d = Math.hypot((cx - x) * r.width, (cy - y) * r.height);
                if (d <= HANDLE_PX && (best === null || d < best.d)) best = { i, d };
            });
            if (best === null) return;
            st.drag = best.i;
            canvas.setPointerCapture(e.pointerId);
            e.preventDefault();
        });
        canvas.addEventListener("pointermove", (e) => {
            if (st.drag === null) return;
            const [x, y] = at(e);
            const corners = cornersOf(card);
            corners[st.drag] = [x, y];
            setCorners(card, corners);
        });
        const release = () => { st.drag = null; };
        canvas.addEventListener("pointerup", release);
        canvas.addEventListener("pointercancel", release);
    }

    // Starts the fold's preview (once per card; it stops on its own when the fold closes or the card goes).
    function open(card) {
        if (!foldOf(card)) return;
        bind(card);
        syncFields(card);
        const st = stateOf(card);
        if (!st.timer && !st.busy) tick(card);
    }

    // ── Styles (layout only; colours come from the dashboard's variables) ─────

    if (root.document && root.document.head) {
        const style = root.document.createElement("style");
        style.textContent = `
            .pl-proc-steps { display:grid; gap:8px; margin:10px 0 12px; }
            .pl-proc-row { display:flex; flex-wrap:wrap; align-items:center; gap:6px 12px; min-width:0; }
            .pl-proc-row > .pl-check:first-child { min-width:110px; }
            .pl-proc-num { display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--text-muted); }
            .pl-proc-num .form-input { width:84px; }
            .pl-proc-row .pl-proc-method { width:auto; min-width:150px; }
            .pl-proc-views { margin:2px 0 8px; }
            .pl-proc-pic { cursor:default; }
            .pl-proc-pic.can-drag { cursor:crosshair; }
        `;
        root.document.head.appendChild(style);
    }

    const api = { DEFAULTS, collect, foldBody, open, statusText };
    root.CodeProcessing = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
