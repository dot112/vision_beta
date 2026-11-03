/*
 * Line setup: the "Pattern and checks" page of a camera whose job is Pattern check (role "match").
 *
 * Training: Take a picture (or Live), draw the Reference box on it (the first box starts the draft on the
 * server with that picture as the training picture), up to 3 more Check boxes, a Search area, and paint the
 * Mask (parts to ignore). Add as good adds the held picture as a good sample; Train makes a reference set and
 * fills each check's Min match and Max defect with suggested values. Edit training takes the card's set back
 * into a draft; Start over removes the draft. While the training picture is shown its boxes are drawn and
 * edited; on a Live or held picture the card's settings, saved or not, are checked and the result is drawn.
 *
 * production_lines.js draws the page with foldHtml(), sends readCard() as the camera's match settings, calls
 * bind() once per card and wake() when the card's pictures may start again or its page is shown or left.
 * Runs in node for tests/test_pattern_check_page.py (module.exports), like text_reader.js.
 */
(function (root) {
    "use strict";

    const API = "/api/v1/match";
    const MAX_CHECKS = 4;
    const MAX_SAMPLES = 20;
    const MIN_BOX_PX = 16;          // pattern_training.MIN_BOX_PX
    const SEARCH_MIN = 0.02;        // line_config.MIN_REGION
    const DEFAULT_ANGLE = 15;
    const DEFAULTS = { min_score: 80, max_defect_px: 30 };
    const PREVIEW_DELAY_MS = 300;   // after a change
    const LIVE_MS = 1000;           // a new picture this often while Live
    const MASK_SEND_MS = 500;       // after the last brush stroke
    const CORNER_PX = 12;           // a press this near a corner of a box resizes it
    const BRUSH = { S: 6, M: 14, L: 28 };   // the brush's radius in pixels of the training picture
    const TOOLS = { reference: "Reference", check: "Check", search: "Search area", mask: "Mask" };
    const SENSITIVITIES = { low: "Low", normal: "Normal", high: "High" };
    const COLORS = { reference: "#ffffff", check: "#ffd400", search: "#00c8ff", fits: "rgb(0,200,0)", wrong: "rgb(230,0,0)", spot: "rgb(255,0,0)" };
    const REASONS = { pattern_not_found: "pattern not found", pattern_mismatch: "pattern mismatch", pattern_defect: "pattern defect", pattern_error: "pattern check error" };

    const esc = (value) => String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const opt = (value, label, current) => `<option value="${esc(value)}" ${String(value) === String(current) ? "selected" : ""}>${esc(label)}</option>`;
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const round4 = (v) => Math.round(v * 10000) / 10000;
    const clamp01 = (v) => Math.min(Math.max(v, 0), 1);

    // One side of a box: ordered, inside 0 to 1, at least `min` long (grown away from the picture's edge).
    function side(a, b, min) {
        let lo = round4(clamp01(Math.min(a, b)));
        let hi = round4(clamp01(Math.max(a, b)));
        if (hi - lo < min - 1e-9) {
            hi = round4(lo + min);
            if (hi > 1) { hi = 1; lo = round4(1 - min); }
        }
        // Rounding must not leave it a hair short.
        while (hi - lo < min - 1e-9) { if (hi < 1) hi = round4(hi + 0.0001); else lo = round4(lo - 0.0001); }
        return [lo, hi];
    }

    // A drag from (x0, y0) to (x1, y1) on a picture shown shownW x shownH whose real size is picW x picH: the box
    // [x1, y1, x2, y2] as fractions, each side at least 16 px of the real picture and at least minFraction.
    function boxFrom(x0, y0, x1, y1, shownW, shownH, picW, picH, minFraction = 0) {
        const [left, right] = side(x0 / shownW, x1 / shownW, Math.max(MIN_BOX_PX / picW, minFraction));
        const [top, bottom] = side(y0 / shownH, y1 / shownH, Math.max(MIN_BOX_PX / picH, minFraction));
        return [left, top, right, bottom];
    }

    // A round dab of the mask brush at (x, y) (pixels of the mask), painting (1) or erasing (0); the part
    // outside the mask is left out.
    function paint(mask, w, h, x, y, radius, on) {
        const cx = Math.round(x), cy = Math.round(y), r = Math.max(1, Math.round(radius));
        for (let dy = -r; dy <= r; dy += 1) {
            const yy = cy + dy;
            if (yy < 0 || yy >= h) continue;
            for (let dx = -r; dx <= r; dx += 1) {
                const xx = cx + dx;
                if (xx < 0 || xx >= w || dx * dx + dy * dy > r * r) continue;
                mask[yy * w + xx] = on ? 1 : 0;
            }
        }
    }

    // The form's values -> the match settings the server takes (blanks get the defaults).
    function matchFromValues(values) {
        const v = values || {};
        const whole = (value, fallback) => { const n = parseInt(value, 10); return Number.isFinite(n) ? n : fallback; };
        return {
            reference_id: v.reference_id ? String(v.reference_id) : null,
            search_area: Array.isArray(v.search_area) ? v.search_area : null,
            angle: whole(v.angle, DEFAULT_ANGLE),
            checks: (v.checks || []).map((c) => ({
                name: String(c.name || "").trim(),
                min_score: whole(c.min_score, DEFAULTS.min_score),
                max_defect_px: whole(c.max_defect_px, DEFAULTS.max_defect_px),
                colour: c.colour === "colour" || c.colour === true,
                sensitivity: c.sensitivity || "normal",
            })),
        };
    }

    // Train's suggested thresholds into the checks: only Min match and Max defect change.
    const applySuggestions = (checks, suggested) => (checks || []).map((c, i) => (suggested && suggested[i]
        ? { ...c, min_score: suggested[i].min_score, max_defect_px: suggested[i].max_defect_px } : c));

    // Colour or Sensitivity changed since the last Train of this page: its suggestions no longer fit them.
    function trainAgain(atTrain, now) {
        if (!atTrain) return false;
        return (now || []).some((c, i) => !atTrain[i] || Boolean(c.colour) !== Boolean(atTrain[i].colour)
            || (c.sensitivity || "normal") !== (atTrain[i].sensitivity || "normal"));
    }

    // The name a new check gets: Main for the reference, then Check2, Check3...
    function newCheckName(names) {
        if (!names || !names.length) return "Main";
        const taken = new Set(names.map((n) => String(n).toLowerCase()));
        for (let n = 2; ; n += 1) if (!taken.has(`check${n}`)) return `Check${n}`;
    }

    function checkRowHtml(c, index, readOnly) {
        const dis = readOnly ? "disabled" : "";
        // From the saved settings (true / false) or from the form when the rows are drawn again ("colour" / "grey").
        const colour = c.colour === true || c.colour === "colour" ? "colour" : "grey";
        const value = (key) => (c[key] === undefined || c[key] === null ? DEFAULTS[key] : c[key]);
        return `<tr data-pc-check="${index}">
            <td><input class="form-input" data-pc="name" maxlength="32" value="${esc(c.name || "")}" ${dis}></td>
            <td><input class="form-input" type="number" min="0" max="100" step="1" data-pc="min_score" value="${esc(value("min_score"))}" ${dis}></td>
            <td><input class="form-input" type="number" min="0" max="1000000" step="1" data-pc="max_defect_px" value="${esc(value("max_defect_px"))}" ${dis}></td>
            <td><select class="form-input" data-pc="colour" ${dis}>${opt("grey", "Grey", colour)}${opt("colour", "Colour", colour)}</select></td>
            <td><select class="form-input" data-pc="sensitivity" ${dis}>${Object.keys(SENSITIVITIES).map((k) => opt(k, SENSITIVITIES[k], c.sensitivity || "normal")).join("")}</select></td>
            <td>${index ? `<button data-lock type="button" class="btn-action btn-outline pc-x" data-act="pc-remove-check" title="Remove this check" ${dis}>×</button>` : ""}</td>
        </tr>`;
    }

    // The page's body. match: the camera's saved match settings, or null for a new one.
    function foldHtml(match, opts) {
        const o = opts || {};
        const m = match && typeof match === "object" ? match : {};
        const readOnly = Boolean(o.readOnly);
        const dis = readOnly ? "disabled" : "";
        const checks = (Array.isArray(m.checks) && m.checks.length ? m.checks : [{ name: newCheckName([]) }]).slice(0, MAX_CHECKS);
        const angle = m.angle === undefined || m.angle === null ? DEFAULT_ANGLE : m.angle;
        const tool = (key) => `<button data-lock type="button" class="btn-action btn-outline pc-tool${key === "reference" ? " active" : ""}" data-act="pc-tool" data-tool="${key}" ${dis}>${TOOLS[key]}</button>`;
        return `<div class="pc-body" data-pc-new="${match ? 0 : 1}">
            <input type="hidden" data-f="pc_reference_id" value="${esc(m.reference_id || "")}">
            <input type="hidden" data-f="pc_search_area" value="${esc(m.search_area ? JSON.stringify(m.search_area) : "")}">
            <div class="pc-toolbar">
                <button type="button" class="btn-action btn-outline" data-act="pc-live">Live</button>
                <button type="button" class="btn-action btn-outline" data-act="pc-hold">Take a picture</button>
                <button type="button" class="btn-action btn-outline" data-act="pc-training" hidden>Training picture</button>
                <span class="pc-tools"><span class="pc-label">Draw:</span>${Object.keys(TOOLS).map(tool).join("")}</span>
            </div>
            <div class="pc-brush" hidden><span class="pc-label" data-help="Paint over the parts the check must ignore, such as a date that changes or a shiny spot: the search and the comparison leave them out.">Mask brush:</span>
                <button data-lock type="button" class="btn-action btn-outline pc-paint active" data-act="pc-paint" data-paint="1" ${dis}>Paint</button>
                <button data-lock type="button" class="btn-action btn-outline pc-paint" data-act="pc-paint" data-paint="0" ${dis}>Erase</button>
                <span class="pc-label">Size</span>${Object.keys(BRUSH).map((k) => `<button data-lock type="button" class="btn-action btn-outline pc-size${k === "M" ? " active" : ""}" data-act="pc-size" data-size="${k}" ${dis}>${k}</button>`).join("")}
            </div>
            <div class="pc-pic" data-f="pc-pic"><img alt="" hidden><canvas></canvas><span class="pc-pic-note">No picture yet</span></div>
            <div class="pc-result" data-f="pc-result"></div>
            <div class="pc-row"><span class="pc-label" data-f="pc-samples-title">Good samples</span><span class="pc-samples" data-f="pc-samples"></span>
                <button data-lock type="button" class="btn-action btn-outline" data-act="pc-add" ${dis}>Add as good</button></div>
            <div class="pc-row">
                <button data-lock type="button" class="btn-action" data-act="pc-train" ${dis}>Train</button>
                <span class="pl-note pc-train-note" data-f="pc-train-note">5–20 good samples give better thresholds.</span>
                <button data-lock type="button" class="btn-action btn-outline" data-act="pc-edit" ${dis}>Edit training</button>
                <button data-lock type="button" class="btn-action btn-outline" data-act="pc-start-over" ${dis}>Start over</button>
            </div>
            <div class="pl-warn pc-again" data-f="pc-again" hidden>Colour or Sensitivity changed: Train again for new suggested thresholds.</div>
            <div class="form-group pc-angle"><label class="form-label">Search: angle ± (degrees)</label>
                <input type="number" class="form-input" data-f="pc_angle" min="0" max="45" step="1" value="${esc(angle)}" ${dis}></div>
            <div class="table-responsive"><table class="pl-table pc-checks"><thead><tr><th>Check</th><th>Min match %</th><th>Max defect px</th><th>Colour</th>
                <th><span data-help="How different from the good samples a pixel must be to count as a defect: High finds fainter marks, Low only strong ones.">Sensitivity</span></th><th></th></tr></thead>
                <tbody data-f="pc-checks">${checks.map((c, i) => checkRowHtml(c, i, readOnly)).join("")}</tbody></table></div>
        </div>`;
    }

    // ── The card in the page ──────────────────────────────────────────────────

    const states = new WeakMap();
    const stateOf = (card) => states.get(card);
    const refreshSelects = () => { if (typeof root.initCustomSelects === "function") setTimeout(root.initCustomSelects, 30); };
    const rowsOf = (card) => [...card.querySelectorAll("[data-pc-check]")];
    const pc = (row, key) => row.querySelector(`[data-pc="${key}"]`);
    const field = (card, name) => card.querySelector(`[data-f="${name}"]`);

    function rowValues(row) {
        return { name: pc(row, "name").value, min_score: pc(row, "min_score").value, max_defect_px: pc(row, "max_defect_px").value,
                 colour: pc(row, "colour").value, sensitivity: pc(row, "sensitivity").value };
    }

    // The card's match settings, as Save sends them.
    function readCard(card) {
        let area = null;
        try { area = field(card, "pc_search_area").value ? JSON.parse(field(card, "pc_search_area").value) : null; } catch (_) { area = null; }
        return matchFromValues({ reference_id: field(card, "pc_reference_id").value, search_area: area,
                                 angle: field(card, "pc_angle").value, checks: rowsOf(card).map(rowValues) });
    }

    function foldOpen(card) {
        const body = card.querySelector(".pc-body");
        const fold = body && body.closest(".pl-fold");
        return Boolean(body && (!fold || (fold.classList.contains("open") && !fold.hidden)));
    }

    const active = (card) => card.isConnected && !root.document.hidden && foldOpen(card) && stateOf(card).api.active();
    // Not Level 1, not locked: the draft buttons carry data-lock, which the configuration lock disables.
    const canEdit = (card) => !card.querySelector('[data-act="pc-train"]').disabled;
    const cameraPath = (card, rest = "") => `${API}${rest}/camera/${encodeURIComponent(card.dataset.camera)}`;

    // The card changed here (not by typing): production_lines marks it unsaved; then a new preview.
    function changed(card) {
        card.querySelector(".pc-body").dispatchEvent(new root.Event("change", { bubbles: true }));
    }

    function note(card, message, bad) {
        const result = field(card, "pc-result");
        result.className = `pc-result${bad ? " pc-error" : ""}`;
        result.textContent = message || "";
    }

    function redrawRows(card, checks) {
        field(card, "pc-checks").innerHTML = checks.slice(0, MAX_CHECKS).map((c, i) => checkRowHtml(c, i, Boolean(stateOf(card).readOnly))).join("");
        refreshSelects();
    }

    // One row per box of the draft: the reference, then each check (names kept by their place).
    function syncRows(card) {
        const s = stateOf(card);
        if (!s.draft) return;
        const wanted = 1 + (s.draft.checks || []).length;
        const rows = rowsOf(card).map(rowValues);
        if (rows.length === wanted) return;
        const checks = rows.slice(0, wanted);
        while (checks.length < wanted) checks.push({ name: newCheckName(checks.map((c) => c.name)) });
        redrawRows(card, checks);
    }

    function setMode(card, mode) {
        const s = stateOf(card);
        s.mode = mode;
        card.querySelector('[data-act="pc-live"]').classList.toggle("active", mode === "live");
        card.querySelector('[data-act="pc-training"]').hidden = !s.draft || mode === "training";
    }

    function setTool(card, tool) {
        const s = stateOf(card);
        s.tool = tool;
        card.querySelectorAll(".pc-tool").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
        card.querySelector(".pc-brush").hidden = tool !== "mask";
    }

    function showPicture(card, blob) {
        const pic = field(card, "pc-pic");
        const img = pic.querySelector("img");
        return new Promise((resolve) => {
            const url = URL.createObjectURL(blob);
            const previous = img.dataset.blob;
            img.onload = () => {
                pic.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
                img.hidden = false;
                pic.querySelector(".pc-pic-note").textContent = "";
                if (previous) URL.revokeObjectURL(previous);
                resolve();
            };
            img.onerror = () => resolve();
            img.dataset.blob = url;
            img.src = url;
        });
    }

    // ── Drawing ───────────────────────────────────────────────────────────────

    function strokeBox(ctx, box, w, h, color, label, dashed) {
        const [x1, y1, x2, y2] = box;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        if (dashed) ctx.setLineDash([6, 4]);
        ctx.strokeRect(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h);
        ctx.restore();
        if (label) {
            ctx.fillStyle = color;
            ctx.font = "12px sans-serif";
            ctx.fillText(label, x1 * w + 3, Math.max(12, y1 * h - 4));
        }
    }

    function maskCanvas(s) {
        if (!s.mask || !s.picSize) return null;
        if (s.maskCanvas && !s.maskChanged) return s.maskCanvas;
        const [w, h] = s.picSize;
        const canvas = s.maskCanvas || root.document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        const image = ctx.createImageData(w, h);
        for (let i = 0; i < s.mask.length; i += 1) {
            if (!s.mask[i]) continue;
            image.data[i * 4] = 255;
            image.data[i * 4 + 3] = 90;
        }
        ctx.putImageData(image, 0, 0);
        s.maskCanvas = canvas;
        s.maskChanged = false;
        return canvas;
    }

    function draw(card) {
        const s = stateOf(card);
        const pic = field(card, "pc-pic");
        const canvas = pic.querySelector("canvas");
        const w = pic.clientWidth, h = pic.clientHeight;
        if (!w || !h) return;
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, w, h);
        const names = rowsOf(card).map((row) => pc(row, "name").value);
        if (s.mode === "training" && s.draft) {
            const mask = maskCanvas(s);
            if (mask) ctx.drawImage(mask, 0, 0, w, h);
            const area = readCard(card).search_area;
            if (area) strokeBox(ctx, area, w, h, COLORS.search, "Search area", true);
            if (s.draft.reference) strokeBox(ctx, s.draft.reference, w, h, COLORS.reference, names[0] || "Reference");
            (s.draft.checks || []).forEach((box, i) => strokeBox(ctx, box, w, h, COLORS.check, names[i + 1] || `Check ${i + 2}`));
            return;
        }
        const r = s.last;
        if (!r || !r.width) return;
        const sx = w / r.width, sy = h / r.height;
        ctx.lineWidth = 2;
        (r.checks || []).forEach((c) => {
            ctx.strokeStyle = c.fits ? COLORS.fits : COLORS.wrong;
            ctx.beginPath();
            (c.box || []).forEach(([x, y], i) => (i ? ctx.lineTo(x * sx, y * sy) : ctx.moveTo(x * sx, y * sy)));
            ctx.closePath();
            ctx.stroke();
            ctx.strokeStyle = COLORS.spot;
            (c.spots || []).forEach(([x1, y1, x2, y2]) => ctx.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy));
        });
    }

    function showResult(card) {
        const r = stateOf(card).last;
        if (!r) return;
        if (r.error) { note(card, r.error, true); return; }
        const checks = (r.checks || []).map((c) => `${c.name} ${c.score}% ${c.defect_px} px`).join(" · ");
        const verdict = r.result === "good" ? "GOOD" : `REJECT (${REASONS[r.reason] || r.reason})`;
        note(card, `${verdict}${checks ? ` · ${checks}` : ""} · ${Math.round(r.ms)} ms`, r.result !== "good");
    }

    // ── The server ────────────────────────────────────────────────────────────

    async function preview(card) {
        const s = stateOf(card);
        if (s.mode === "training" || !s.held) return;
        if (!field(card, "pc_reference_id").value) { s.last = null; note(card, "Train the reference to see each picture's result."); draw(card); return; }
        if (s.busy) { s.again = true; return; }
        s.busy = true;
        try {
            s.last = await s.api.fetchJson(`${cameraPath(card, "/preview")}`, { method: "POST", body: { settings: s.api.entry(), held: true } });
            showResult(card);
        } catch (e) {
            s.last = null;
            note(card, e.message, true);
        } finally {
            s.busy = false;
        }
        draw(card);
        if (s.again) { s.again = false; preview(card); }
    }

    function previewSoon(card) {
        const s = stateOf(card);
        clearTimeout(s.timer);
        s.timer = setTimeout(() => preview(card), PREVIEW_DELAY_MS);
    }

    async function takePicture(card) {
        const s = stateOf(card);
        const id = card.dataset.camera;
        try {
            const blob = await s.api.fetchBlob(cameraPath(card, "/picture"), { method: "POST" });
            if (card.dataset.camera !== id) return;
            await showPicture(card, blob);
            s.held = id;
        } catch (e) {
            s.held = null;
            s.last = null;
            field(card, "pc-pic").querySelector(".pc-pic-note").textContent = e.message;
            draw(card);
            return;
        }
        await preview(card);
    }

    async function runLive(card) {
        const s = stateOf(card);
        if (s.running) return;
        s.running = true;
        while (s.mode === "live" && active(card)) {
            const asked = Date.now();
            await takePicture(card);
            await sleep(Math.max(200, LIVE_MS - (Date.now() - asked)));
        }
        s.running = false;
    }

    async function loadMask(card) {
        const s = stateOf(card);
        const [w, h] = s.picSize;
        s.mask = new Uint8Array(w * h);
        s.maskChanged = true;
        try {
            const blob = await s.api.fetchBlob(`${cameraPath(card, "/draft")}/mask`);
            const bitmap = await root.createImageBitmap(blob);
            const canvas = root.document.createElement("canvas");
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext("2d");
            ctx.drawImage(bitmap, 0, 0);
            const data = ctx.getImageData(0, 0, w, h).data;
            for (let i = 0; i < s.mask.length; i += 1) s.mask[i] = data[i * 4] > 0 ? 1 : 0;
        } catch (_) { /* no mask painted yet */ }
    }

    async function renderSamples(card) {
        const s = stateOf(card);
        const box = field(card, "pc-samples");
        const samples = (s.draft && s.draft.samples) || [];
        field(card, "pc-samples-title").textContent = `Good samples (${samples.length})`;
        box.innerHTML = samples.map((sample) => `<span class="pc-sample"><img alt="Sample ${sample.n}" data-n="${sample.n}"><b>${esc(sample.score)}%</b>${sample.n > 1
            ? `<button data-lock type="button" class="pc-x" data-act="pc-remove-sample" data-n="${sample.n}" title="Remove this sample" ${canEdit(card) ? "" : "disabled"}>×</button>` : ""}</span>`).join("");
        for (const img of box.querySelectorAll("img")) {
            try {
                const blob = await s.api.fetchBlob(`${cameraPath(card, "/draft")}/samples/${img.dataset.n}`);
                img.src = URL.createObjectURL(blob);
            } catch (_) { /* the sample was removed meanwhile */ }
        }
    }

    // The draft as the server has it; the training picture shown for drawing on when there is one.
    async function applyDraft(card, draft) {
        const s = stateOf(card);
        s.draft = draft;
        if (!draft) { s.mask = null; s.picSize = null; setMode(card, s.mode === "training" ? "live" : s.mode); renderSamples(card); return; }
        s.picSize = draft.picture_size;
        if (draft.search_area && !field(card, "pc_search_area").value) field(card, "pc_search_area").value = JSON.stringify(draft.search_area);
        syncRows(card);
        await loadMask(card);
        renderSamples(card);
    }

    async function loadDraft(card) {
        const s = stateOf(card);
        try {
            await applyDraft(card, await s.api.fetchJson(cameraPath(card, "/draft")));
        } catch (_) {
            await applyDraft(card, null);
        }
        s.loaded = true;
    }

    async function showTraining(card) {
        const s = stateOf(card);
        if (!s.draft) return;
        setMode(card, "training");
        try {
            await showPicture(card, await s.api.fetchBlob(`${cameraPath(card, "/draft")}/picture`));
        } catch (e) { note(card, e.message, true); }
        note(card, "Drag on the picture to draw the selected box; drag inside a box to move it, by a corner to resize it.");
        draw(card);
    }

    async function saveBoxes(card) {
        const s = stateOf(card);
        const body = { reference: s.draft ? s.draft.reference : null, checks: s.draft ? s.draft.checks : [],
                       search_area: readCard(card).search_area };
        try {
            const draft = await s.api.fetchJson(cameraPath(card, "/draft"), { method: "PUT", body });
            const fresh = !s.draft;
            await applyDraft(card, draft);
            if (fresh) await showTraining(card);
        } catch (e) { note(card, e.message, true); }
        draw(card);
    }

    function sendMaskSoon(card) {
        const s = stateOf(card);
        clearTimeout(s.maskTimer);
        s.maskTimer = setTimeout(() => {
            const canvas = root.document.createElement("canvas");
            const [w, h] = s.picSize;
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext("2d");
            const image = ctx.createImageData(w, h);
            for (let i = 0; i < s.mask.length; i += 1) {
                const v = s.mask[i] ? 255 : 0;
                image.data[i * 4] = image.data[i * 4 + 1] = image.data[i * 4 + 2] = v;
                image.data[i * 4 + 3] = 255;
            }
            ctx.putImageData(image, 0, 0);
            canvas.toBlob((blob) => s.api.sendBlob(`${cameraPath(card, "/draft")}/mask`, blob).catch((e) => note(card, e.message, true)), "image/png");
        }, MASK_SEND_MS);
    }

    async function addSample(card) {
        const s = stateOf(card);
        if (s.mode === "training" || !s.held) { note(card, "Take a picture of another good product first.", true); return; }
        try {
            const added = await s.api.fetchJson(`${cameraPath(card, "/draft")}/samples`, { method: "POST", body: { angle: readCard(card).angle } });
            note(card, `Sample ${added.n} added: found at ${Math.round(added.found.score * 100)}% of the training picture.`);
            await loadDraft(card);
        } catch (e) { note(card, e.message, true); }
    }

    async function train(card) {
        const s = stateOf(card);
        const now = readCard(card);
        try {
            const info = await s.api.fetchJson(`${cameraPath(card, "/draft")}/train`, {
                method: "POST", body: { angle: now.angle, checks: now.checks.map((c) => ({ colour: c.colour, sensitivity: c.sensitivity })) } });
            field(card, "pc_reference_id").value = info.reference_id;
            redrawRows(card, applySuggestions(rowsOf(card).map(rowValues), info.suggested));
            s.atTrain = now.checks.map((c) => ({ colour: c.colour, sensitivity: c.sensitivity }));
            field(card, "pc-again").hidden = true;
            field(card, "pc-train-note").textContent = info.note || `Thresholds suggested from ${info.samples.length} samples. Save to keep them.`;
            changed(card);
            await loadDraft(card);
            note(card, "Trained. Take a picture or watch Live to see each product's result.");
        } catch (e) { note(card, e.message, true); }
    }

    // ── Drawing boxes and the mask on the training picture ────────────────────

    function bindDrawing(card) {
        const pic = field(card, "pc-pic");
        let drag = null;
        const at = (e) => { const r = pic.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width, h: r.height }; };
        const boxOf = (s, tool, index) => (tool === "reference" ? s.draft && s.draft.reference
            : tool === "search" ? readCard(card).search_area : s.draft && (s.draft.checks || [])[index]);
        const setBox = (s, tool, index, box) => {
            if (tool === "search") field(card, "pc_search_area").value = JSON.stringify(box);
            else if (tool === "reference") { s.draft = s.draft || { checks: [] }; s.draft.reference = box; }
            else { s.draft.checks = s.draft.checks || []; s.draft.checks[index] = box; }
        };
        const near = (p, x, y) => Math.abs(p.x - x) <= CORNER_PX && Math.abs(p.y - y) <= CORNER_PX;
        pic.addEventListener("pointerdown", (e) => {
            const s = stateOf(card);
            if (!canEdit(card) || !pic.querySelector("img").naturalWidth) return;
            const p = at(e);
            if (s.mode !== "training") {
                // The first box starts the draft on the picture held now (a Live picture is held as it shows).
                if (s.draft || s.tool !== "reference" || !s.held) return;
                setMode(card, "held");
            }
            const img = pic.querySelector("img");
            const picW = s.picSize ? s.picSize[0] : img.naturalWidth, picH = s.picSize ? s.picSize[1] : img.naturalHeight;
            if (s.tool === "mask") {
                if (!s.mask) return;
                drag = { mask: true, paint: s.paint, picW, picH };
                paint(s.mask, picW, picH, p.x / p.w * picW, p.y / p.h * picH, BRUSH[s.brush], s.paint);
                s.maskChanged = true;
                draw(card);
            } else {
                let index = 0;
                if (s.tool === "check") {
                    const checks = (s.draft && s.draft.checks) || [];
                    index = checks.findIndex((b) => p.x >= b[0] * p.w - CORNER_PX && p.x <= b[2] * p.w + CORNER_PX
                        && p.y >= b[1] * p.h - CORNER_PX && p.y <= b[3] * p.h + CORNER_PX);
                    if (index < 0) {
                        if (!s.draft || !s.draft.reference) { note(card, "Draw the Reference box first.", true); return; }
                        if (checks.length >= MAX_CHECKS - 1) { note(card, `At most ${MAX_CHECKS - 1} checks besides the reference.`, true); return; }
                        index = checks.length;
                    }
                }
                const box = boxOf(s, s.tool, index);
                const min = s.tool === "search" ? SEARCH_MIN : 0;
                let mode = "new", anchor = { x: p.x, y: p.y };
                if (box) {
                    const [x1, y1, x2, y2] = [box[0] * p.w, box[1] * p.h, box[2] * p.w, box[3] * p.h];
                    const corners = [[x1, y1, x2, y2], [x2, y1, x1, y2], [x1, y2, x2, y1], [x2, y2, x1, y1]];
                    const corner = corners.find(([cx, cy]) => near(p, cx, cy));
                    if (corner) { mode = "resize"; anchor = { x: corner[2], y: corner[3] }; }
                    else if (p.x > x1 && p.x < x2 && p.y > y1 && p.y < y2) mode = "move";
                }
                drag = { tool: s.tool, index, mode, anchor, start: p, box, min, picW, picH, isNew: !box };
            }
            pic.setPointerCapture(e.pointerId);
            e.preventDefault();
        });
        pic.addEventListener("pointermove", (e) => {
            if (!drag) return;
            const s = stateOf(card);
            const p = at(e);
            if (drag.mask) {
                paint(s.mask, drag.picW, drag.picH, p.x / p.w * drag.picW, p.y / p.h * drag.picH, BRUSH[s.brush], drag.paint);
                s.maskChanged = true;
            } else if (drag.mode === "move") {
                const [x1, y1, x2, y2] = drag.box;
                const dx = Math.min(Math.max((p.x - drag.start.x) / p.w, -x1), 1 - x2);
                const dy = Math.min(Math.max((p.y - drag.start.y) / p.h, -y1), 1 - y2);
                setBox(s, drag.tool, drag.index, [x1 + dx, y1 + dy, x2 + dx, y2 + dy].map(round4));
            } else {
                setBox(s, drag.tool, drag.index, boxFrom(drag.anchor.x, drag.anchor.y, p.x, p.y, p.w, p.h, drag.picW, drag.picH, drag.min));
            }
            draw(card);
        });
        const end = () => {
            if (!drag) return;
            const done = drag;
            drag = null;
            const s = stateOf(card);
            if (done.mask) { sendMaskSoon(card); return; }
            if (done.tool === "check" && done.isNew) {
                const checks = rowsOf(card).map(rowValues);
                checks.push({ name: newCheckName(checks.map((c) => c.name)) });
                redrawRows(card, checks);
            }
            if (done.tool === "search") changed(card);
            if (s.draft || done.tool === "reference") saveBoxes(card);
        };
        pic.addEventListener("pointerup", end);
        pic.addEventListener("pointercancel", end);
    }

    // The card's pictures start again where they may: Live while the page is shown on a Pattern check card.
    async function wake(card) {
        const s = stateOf(card);
        if (!s) return;
        if (s.held && s.held !== card.dataset.camera) { s.held = null; s.last = null; s.loaded = false; s.draft = null; setMode(card, "live"); }
        if (!active(card)) return;
        if (!s.loaded) {
            await loadDraft(card);
            if (s.draft) { await showTraining(card); return; }
        }
        if (s.mode === "live") runLive(card);
        else draw(card);
    }

    // Wires one card. api: fetchJson(path, options), fetchBlob(path, options), sendBlob(path, blob) (PUT a PNG),
    // entry() (the card's camera entry), active() (a Pattern check card on the shown Line setup) and readOnly.
    function bind(card, api) {
        const s = { api, readOnly: Boolean(api.readOnly), mode: "live", tool: "reference", paint: true, brush: "M", draft: null,
                    mask: null, picSize: null, held: null, last: null, busy: false, again: false, running: false, loaded: false,
                    atTrain: null, timer: null, maskTimer: null };
        states.set(card, s);
        const body = card.querySelector(".pc-body");
        body.addEventListener("click", async (e) => {
            const button = e.target.closest("[data-act]");
            if (!button || !body.contains(button)) return;
            const act = button.dataset.act;
            if (act === "pc-live") { setMode(card, "live"); runLive(card); }
            else if (act === "pc-hold") { setMode(card, "held"); takePicture(card); }
            else if (act === "pc-training") showTraining(card);
            else if (act === "pc-tool") setTool(card, button.dataset.tool);
            else if (act === "pc-paint") { s.paint = button.dataset.paint === "1"; card.querySelectorAll(".pc-paint").forEach((b) => b.classList.toggle("active", b === button)); }
            else if (act === "pc-size") { s.brush = button.dataset.size; card.querySelectorAll(".pc-size").forEach((b) => b.classList.toggle("active", b === button)); }
            else if (act === "pc-add") addSample(card);
            else if (act === "pc-train") train(card);
            else if (act === "pc-edit") {
                const id = field(card, "pc_reference_id").value;
                if (!id) { note(card, "This camera has no trained reference yet.", true); return; }
                try { await applyDraft(card, await api.fetchJson(`${cameraPath(card, "/draft")}/edit`, { method: "POST", body: { reference_id: id } })); await showTraining(card); }
                catch (err) { note(card, err.message, true); }
            } else if (act === "pc-start-over") {
                try { await api.fetchJson(cameraPath(card, "/draft"), { method: "DELETE" }); } catch (err) { note(card, err.message, true); return; }
                await applyDraft(card, null);
                setMode(card, "live");
                runLive(card);
            } else if (act === "pc-remove-sample") {
                try { await applyDraft(card, await api.fetchJson(`${cameraPath(card, "/draft")}/samples/${button.dataset.n}`, { method: "DELETE" })); }
                catch (err) { note(card, err.message, true); }
            } else if (act === "pc-remove-check") {
                const row = button.closest("[data-pc-check]");
                const index = rowsOf(card).indexOf(row);
                if (index < 1) return;
                redrawRows(card, rowsOf(card).map(rowValues).filter((_, i) => i !== index));
                if (s.draft && s.draft.checks) { s.draft.checks.splice(index - 1, 1); saveBoxes(card); }
                changed(card);
            }
        });
        body.addEventListener("change", () => {
            field(card, "pc-again").hidden = !trainAgain(s.atTrain, readCard(card).checks);
            draw(card);
            previewSoon(card);
        });
        body.addEventListener("input", () => { draw(card); previewSoon(card); });
        bindDrawing(card);
        setMode(card, "live");
        wake(card);
    }

    // ── The Line dashboard and the Records page ───────────────────────────────

    // The Latest pattern checks panel; production_lines.js fills plPatternBody with checksRowsHtml().
    function checksPanelHtml() {
        return `<div class="card-panel" id="plPatternPanel"><div class="card-panel-header"><span class="card-panel-title">Latest pattern checks</span></div>
                <div class="table-responsive"><table class="pl-table"><thead><tr><th>Time</th><th>Camera</th><th>Checks</th><th>Product result</th></tr></thead>
                <tbody id="plPatternBody"><tr><td colspan="4">No checks yet.</td></tr></tbody></table></div></div>`;
    }

    // GET /lines/{id}/match/recent's checks as table rows. opts: name(cameraId) -> its name, reasons {reason: label}.
    function checksRowsHtml(checks, opts) {
        const o = opts || {};
        const name = (id) => (o.name ? o.name(id) : id) || id;
        const reasons = o.reasons || REASONS;
        const rows = (checks || []).map((r) => {
            const parts = (r.checks || []).map((c) => `<div class="${c.fits ? "pc-fits" : "pc-wrong"}">${esc(c.name)} ${esc(c.score)}% · ${esc(c.defect_px)} px</div>`).join("")
                || (r.error ? `<div class="pc-wrong">${esc(r.error)}</div>` : "");
            const verdict = r.result === "reject" ? `<b style="color:var(--danger-color)">Rejected</b>${r.reason ? ` · ${esc(reasons[r.reason] || r.reason)}` : ""}`
                : r.result === "good" ? `<b style="color:var(--success-color)">Good</b>` : `<span class="pc-none">—</span>`;
            const product = r.track_id !== null && r.track_id !== undefined ? `<div class="pc-none">product #${esc(r.track_id)}</div>` : "";
            return `<tr><td class="pl-code">${esc(new Date(r.timestamp * (r.timestamp < 1e11 ? 1000 : 1)).toLocaleTimeString())}</td><td>${esc(name(r.camera_id))}</td><td>${parts}</td><td>${verdict}${product}</td></tr>`;
        });
        return rows.join("") || `<tr><td colspan="4">No checks yet.</td></tr>`;
    }

    // A record's pattern check (details.match): each check's Match % and biggest defect; "" without one.
    function recordText(details) {
        const match = details && typeof details.match === "object" && details.match ? details.match : null;
        if (!match) return "";
        const checks = match.checks && typeof match.checks === "object" ? match.checks : {};
        const parts = Object.keys(checks).map((k) => `${esc(k)} ${esc(checks[k].score)}% ${esc(checks[k].defect_px)} px`);
        if (!parts.length) return match.status === "error" ? `<div class="pc-record pc-wrong">pattern check error</div>` : "";
        return `<div class="pc-record">${parts.join(" · ")}</div>`;
    }

    const api = { MAX_CHECKS, MAX_SAMPLES, MIN_BOX_PX, foldHtml, matchFromValues, readCard, boxFrom, paint, applySuggestions,
                  trainAgain, newCheckName, bind, wake, checksPanelHtml, checksRowsHtml, recordText };
    root.PatternCheck = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
