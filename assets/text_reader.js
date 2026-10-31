/*
 * Line setup: the "Text fields" fold of a camera card whose job is Text reader (role "ocr").
 *
 * The reader's settings (model, Join dots and its print, lowest confidence, text upside down, the
 * YOLO model whose boxes the fields read in), up to four text fields (name, where the text is found,
 * format), and the camera's picture: Live (a new one every second) or held (Take a picture keeps one
 * still). What the card's settings read, saved or not, is drawn over the picture after each change
 * and with each live picture (POST /api/v1/ocr/preview/camera/{id}). Read lists every line found; a
 * click on one gives the selected field that line's format. A Region field's region is drawn by
 * dragging on the picture, and moved or resized by its corners.
 *
 * production_lines.js draws the fold with foldHtml(), sends readCard() as the camera's ocr settings,
 * calls bind() once per card and wake() when the card's pictures may start again.
 * Runs in node for tests/test_text_reader_card.py (module.exports), like data_backups.js.
 */
(function (root) {
    "use strict";

    const MAX_FIELDS = 4;
    const MODEL_LABELS = { tiny: "Tiny: fastest", small: "Small", medium: "Medium: slowest, reads the most" };
    const FINDS = { region: "Region", yolo: "YOLO class", all: "Whole picture" };
    const JOIN_DOTS = [3, 5, 7, 9, 11, 13, 15];
    const DEFAULT_CONFIDENCE = 0.5;
    const NEW_REGION = [0.25, 0.25, 0.75, 0.5];
    const MIN_SIDE = 0.02;          // line_config.MIN_REGION
    const PREVIEW_DELAY_MS = 300;   // after a change
    const LIVE_MS = 1000;           // a new picture this often while Live
    const CORNER_PX = 12;           // a press this near a corner of the region resizes it
    const COLORS = { area: "rgba(200,200,200,.9)", region: "#ffffff", fits: "rgb(0,200,0)", wrong: "rgb(230,0,0)", line: "rgb(0,120,255)" };
    const REASONS = { text_wrong: "the text is wrong", no_text: "no text found", reader_error: "the reader could not read" };

    const esc = (value) => String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const opt = (value, label, current) => `<option value="${esc(value)}" ${String(value) === String(current) ? "selected" : ""}>${esc(label)}</option>`;
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const round4 = (v) => Math.round(v * 10000) / 10000;
    const clamp01 = (v) => Math.min(Math.max(v, 0), 1);

    // The format a reading suggests: each digit becomes 9 (text_format.suggest_format).
    const suggestFormat = (text) => String(text === undefined || text === null ? "" : text).trim().toUpperCase().replace(/[0-9]/g, "9");

    // One side of a region: ordered, inside 0 to 1, at least MIN_SIDE long (grown away from the picture's edge).
    function side(a, b) {
        let lo = round4(clamp01(Math.min(a, b)));
        let hi = round4(clamp01(Math.max(a, b)));
        if (hi - lo < MIN_SIDE) {
            hi = round4(lo + MIN_SIDE);
            if (hi > 1) { hi = 1; lo = round4(1 - MIN_SIDE); }
        }
        // Rounding must not leave it a hair short: the server checks x2 - x1 >= MIN_SIDE.
        while (hi - lo < MIN_SIDE) { if (hi < 1) hi = round4(hi + 0.0001); else lo = round4(lo - 0.0001); }
        return [lo, hi];
    }

    // A drag from (x0, y0) to (x1, y1) on a picture width x height: the region [x1, y1, x2, y2] as fractions.
    function regionFrom(x0, y0, x1, y1, width, height) {
        const [left, right] = side(x0 / width, x1 / width);
        const [top, bottom] = side(y0 / height, y1 / height);
        return [left, top, right, bottom];
    }

    // The fold's values to the ocr settings the server takes (line_config.normalize_text_reader).
    function ocrFromValues(values) {
        const v = values || {};
        const join = parseInt(v.join_dots, 10);
        const confidence = parseFloat(v.min_confidence);
        const fields = (v.fields || []).map((f) => {
            const field = { name: String(f.name || "").trim(), find: FINDS[f.find] ? f.find : "all" };
            if (field.find === "region") field.region = Array.isArray(f.region) && f.region.length === 4 ? f.region.map(Number) : NEW_REGION.slice();
            if (field.find === "yolo") field.yolo_class = String(f.yolo_class || "");
            field.format = String(f.format || "").trim();
            return field;
        });
        return {
            model: v.model || "small",
            locator_model_id: v.locator || null,
            upside_down: Boolean(v.upside_down),
            join_dots: Number.isFinite(join) ? join : 0,
            ink: v.ink === "light" ? "light" : "dark",
            min_confidence: Number.isFinite(confidence) ? confidence : DEFAULT_CONFIDENCE,
            fields,
        };
    }

    // A new field's name: Text1, Text2, ... the first one the card does not have.
    function newFieldName(names) {
        const taken = new Set(names.map((n) => String(n).toLowerCase()));
        let n = 1;
        while (taken.has(`text${n}`)) n += 1;
        return `Text${n}`;
    }

    function classesOf(yoloModels, locator) {
        const model = (yoloModels || []).find((m) => m.id === locator);
        return model && Array.isArray(model.classes) ? model.classes.map(String) : [];
    }

    function locatorOptions(yoloModels, locator) {
        const models = (yoloModels || []).slice();
        if (locator && !models.some((m) => m.id === locator)) models.push({ id: locator, name: "(deleted model)", version: "" });
        return opt("", "— Choose a YOLO model —", locator) + models.map((m) => opt(m.id, `${m.name} ${m.version || ""}`.trim(), locator)).join("");
    }

    function classOptions(classes, current) {
        const all = !current || classes.includes(current) ? classes : classes.concat([current]);
        return opt("", "— Choose a class —", current) + all.map((c) => opt(c, c, current)).join("");
    }

    // One field row: name, Find, Format and remove; under them its YOLO class or its region note.
    function fieldHtml(f, index, ctx) {
        const find = FINDS[f.find] ? f.find : "all";
        const region = Array.isArray(f.region) && f.region.length === 4 ? f.region : NEW_REGION;
        return `<div class="tr-field${index === ctx.selected ? " selected" : ""}" data-text-field="${index}" data-region="${region.map(Number).join(",")}">
                <input type="text" class="form-input" data-tf="name" maxlength="32" value="${esc(f.name || "")}" aria-label="Field name">
                <div class="tr-cell"><select class="form-input" data-tf="find" aria-label="Where the text is">${Object.keys(FINDS).map((k) => opt(k, FINDS[k], find)).join("")}</select></div>
                <input type="text" class="form-input tr-format" data-tf="format" maxlength="64" value="${esc(f.format || "")}" placeholder="LOT A99999" aria-label="Format">
                <button data-lock type="button" class="pl-icon-btn danger" data-act="tr-remove" title="Remove this field" aria-label="Remove this field" ${ctx.count <= 1 || ctx.readOnly ? "disabled" : ""}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
                <div class="tr-sub tr-class" ${find === "yolo" ? "" : "hidden"}><select class="form-input" data-tf="yolo_class" aria-label="YOLO class">${classOptions(ctx.classes, f.yolo_class || "")}</select></div>
                <div class="tr-sub pl-note tr-region-note" ${find === "region" ? "" : "hidden"}>Select this field, then drag its region on the picture.</div>
            </div>`;
    }

    const fieldsHtml = (fields, ctx) => fields.map((f, i) => fieldHtml(f, i, { ...ctx, count: fields.length })).join("");

    // The fold's body. ``ocr`` is the camera's saved ocr settings (null for a camera that is not a text reader yet).
    // opts: models (the reader models on this server), newModel (a new reader's), yoloModels [{id, name, version,
    // classes}], readOnly, and ready / error (whether the reader can run here).
    function foldHtml(ocr, opts) {
        const o = opts || {};
        const saved = ocr && typeof ocr === "object" ? ocr : null;
        const models = o.models && o.models.length ? o.models : ["small"];
        const model = (saved && saved.model) || o.newModel || "small";
        const modelOptions = models.map((m) => opt(m, MODEL_LABELS[m] || m, model)).join("")
            + (models.includes(model) ? "" : opt(model, `${MODEL_LABELS[model] || model} (not on this server)`, model));
        const fields = (saved && Array.isArray(saved.fields) && saved.fields.length ? saved.fields : [{ name: newFieldName([]), find: "all", format: "" }]).slice(0, MAX_FIELDS);
        const join = Number(saved && saved.join_dots) || 0;
        const ink = saved && saved.ink === "light" ? "light" : "dark";
        const locator = (saved && saved.locator_model_id) || "";
        const confidence = saved && saved.min_confidence !== undefined && saved.min_confidence !== null ? saved.min_confidence : DEFAULT_CONFIDENCE;
        const readOnly = Boolean(o.readOnly);
        const ctx = { classes: classesOf(o.yoloModels, locator), readOnly, selected: 0 };
        return `<div class="tr-body" data-tr-new="${saved ? 0 : 1}">
            ${o.ready === false ? `<div class="pl-warn tr-not-ready">The text reader cannot run on this server: ${esc(o.error || "it is not installed")}</div>` : ""}
            <div class="pl-form-grid pl-form-tight">
                <div class="form-group"><label class="form-label">Reader model</label><select class="form-input" data-f="ocr_model">${modelOptions}</select></div>
                <div class="form-group"><label class="form-label">Lowest confidence</label><input type="number" class="form-input" data-f="ocr_min_confidence" min="0.3" max="0.99" step="0.01" value="${esc(confidence)}"></div>
                <div class="form-group"><label class="form-label">Join dots</label><select class="form-input" data-f="ocr_join_dots" data-help="Joins the dots of inkjet dot-matrix print into strokes before reading: the size of the gaps it closes.">${opt(0, "Off", join)}${JOIN_DOTS.map((n) => opt(n, `${n} px`, join)).join("")}</select></div>
                <div class="form-group tr-ink" ${join ? "" : "hidden"}><label class="form-label">Print</label><select class="form-input" data-f="ocr_ink">${opt("dark", "Dark print on a light surface", ink)}${opt("light", "Light print on a dark surface", ink)}</select></div>
            </div>
            <label class="pl-inline pl-check"><input type="checkbox" data-f="ocr_upside_down" ${saved && saved.upside_down ? "checked" : ""}> The text can be upside down</label>
            <div class="pl-note">It is read turned round too, which is a little slower.</div>
            <div class="form-group tr-locator" ${fields.some((f) => f.find === "yolo") ? "" : "hidden"}><label class="form-label">YOLO model</label><select class="form-input" data-f="ocr_locator">${locatorOptions(o.yoloModels, locator)}</select>
                <div class="pl-note">The model whose boxes the YOLO class fields read in. Models are uploaded on the AI models page.</div></div>
            <div class="form-label tr-title">Text fields</div>
            <div class="tr-head"><span>Name</span><span>Find</span><span><span data-help="Each character of the text: 9 a digit, A a letter, X either, * any character, and any other character as it is (spaces are ignored).">Format</span></span><span></span></div>
            <div class="tr-fields">${fieldsHtml(fields, ctx)}</div>
            <button data-lock type="button" class="btn-action btn-outline tr-add" data-act="tr-add" ${fields.length >= MAX_FIELDS || readOnly ? "disabled" : ""}>Add field</button>
            <div class="tr-pic" data-f="tr-pic"><img alt="" hidden><canvas></canvas><span class="tr-pic-note">No picture yet</span></div>
            <div class="tr-buttons">
                <button type="button" class="btn-action btn-outline" data-act="tr-live">Live</button>
                <button type="button" class="btn-action btn-outline" data-act="tr-hold">Take a picture</button>
                <button type="button" class="btn-action btn-outline" data-act="tr-read">Read</button>
            </div>
            <div class="tr-result" data-f="tr-result"></div>
            <div class="tr-lines" hidden></div>
        </div>`;
    }

    // ── The card in the page ──────────────────────────────────────────────────

    const rowsOf = (card) => [...card.querySelectorAll(".tr-field")];
    const tf = (row, name) => row.querySelector(`[data-tf="${name}"]`);
    const rowRegion = (row) => String(row.dataset.region || "").split(",").map(Number);

    function rowValues(row) {
        return { name: tf(row, "name").value, find: tf(row, "find").value, yolo_class: tf(row, "yolo_class").value,
                 region: rowRegion(row), format: tf(row, "format").value };
    }

    // The card's ocr settings as the server takes them.
    function readCard(card) {
        const value = (name) => { const el = card.querySelector(`[data-f="${name}"]`); return el ? el.value : ""; };
        const upsideDown = card.querySelector('[data-f="ocr_upside_down"]');
        return ocrFromValues({
            model: value("ocr_model"), locator: value("ocr_locator"), upside_down: Boolean(upsideDown && upsideDown.checked),
            join_dots: value("ocr_join_dots"), ink: value("ocr_ink"), min_confidence: value("ocr_min_confidence"),
            fields: rowsOf(card).map(rowValues),
        });
    }

    const states = new WeakMap();
    const stateOf = (card) => states.get(card);

    const refreshSelect = (select) => { if (select && select._refreshCustomSelect) select._refreshCustomSelect(); };
    const refreshSelects = () => { if (typeof root.initCustomSelects === "function") setTimeout(root.initCustomSelects, 30); };

    function foldOpen(card) {
        const body = card.querySelector(".tr-body");
        const fold = body && body.closest(".pl-fold");
        return Boolean(body && (!fold || (fold.classList.contains("open") && !fold.hidden)));
    }

    const active = (card) => card.isConnected && !root.document.hidden && foldOpen(card) && stateOf(card).api.active();
    const selectedRow = (card) => card.querySelector(".tr-field.selected") || rowsOf(card)[0] || null;
    const canEdit = (card) => { const row = rowsOf(card)[0]; return Boolean(row && !tf(row, "format").disabled); };

    function selectRow(card, row) {
        rowsOf(card).forEach((r) => r.classList.toggle("selected", r === row));
        draw(card);
    }

    // The card changed here (not by typing): production_lines marks it unsaved; then a new preview.
    function changed(card) {
        card.querySelector(".tr-body").dispatchEvent(new root.Event("change", { bubbles: true }));
    }

    // Draw the fields again (a field added or removed), keeping the one selected.
    function redrawFields(card, fields, selected) {
        const s = stateOf(card);
        const readOnly = Boolean(s.readOnly);
        const locator = card.querySelector('[data-f="ocr_locator"]').value;
        card.querySelector(".tr-fields").innerHTML = fieldsHtml(fields, { classes: classesOf(s.api.yoloModels(), locator), readOnly, selected });
        card.querySelector('[data-act="tr-add"]').disabled = fields.length >= MAX_FIELDS || readOnly;
        syncFields(card);
        refreshSelects();
    }

    // Which parts of the fold show: the YOLO model while a field finds by class, Print while Join dots is on.
    function syncFields(card) {
        const rows = rowsOf(card);
        rows.forEach((row) => {
            const find = tf(row, "find").value;
            row.querySelector(".tr-class").hidden = find !== "yolo";
            row.querySelector(".tr-region-note").hidden = find !== "region";
        });
        card.querySelector(".tr-locator").hidden = !rows.some((row) => tf(row, "find").value === "yolo");
        card.querySelector(".tr-ink").hidden = !(parseInt(card.querySelector('[data-f="ocr_join_dots"]').value, 10) > 0);
    }

    function addField(card, format) {
        const fields = rowsOf(card).map(rowValues);
        if (fields.length >= MAX_FIELDS) return false;
        fields.push({ name: newFieldName(fields.map((f) => f.name)), find: "all", format: format || "" });
        redrawFields(card, fields, fields.length - 1);
        return true;
    }

    // ── The picture and the reading drawn over it ─────────────────────────────

    function draw(card) {
        const s = stateOf(card);
        const pic = card.querySelector('[data-f="tr-pic"]');
        const canvas = pic.querySelector("canvas");
        const w = pic.clientWidth;
        const h = pic.clientHeight;
        if (!w || !h) return;
        const ratio = root.devicePixelRatio || 1;
        canvas.width = Math.round(w * ratio);
        canvas.height = Math.round(h * ratio);
        const ctx = canvas.getContext("2d");
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        ctx.clearRect(0, 0, w, h);
        const r = s.last;
        if (r && r.width && r.height) {
            const sx = w / r.width;
            const sy = h / r.height;
            const outline = (box) => {
                ctx.beginPath();
                box.forEach(([x, y], i) => (i ? ctx.lineTo(x * sx, y * sy) : ctx.moveTo(x * sx, y * sy)));
                ctx.closePath();
                ctx.stroke();
            };
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 3]);
            ctx.strokeStyle = COLORS.area;
            (r.fields || []).forEach((f) => {
                const a = f.area;
                if (a && !(a[0] === 0 && a[1] === 0 && a[2] === r.width && a[3] === r.height)) ctx.strokeRect(a[0] * sx, a[1] * sy, (a[2] - a[0]) * sx, (a[3] - a[1]) * sy);
            });
            ctx.setLineDash([]);
            ctx.strokeStyle = COLORS.line;
            (r.lines || []).forEach((line) => { if (line.box) outline(line.box); });
            ctx.lineWidth = 2;
            ctx.font = "bold 12px sans-serif";
            (r.fields || []).forEach((f) => {
                if (!f.box) return;
                const color = f.fits ? COLORS.fits : COLORS.wrong;
                ctx.strokeStyle = color;
                outline(f.box);
                const label = `${f.name}: ${f.text || ""}`;
                const x = Math.min(...f.box.map((p) => p[0])) * sx;
                const y = Math.max(14, Math.min(...f.box.map((p) => p[1])) * sy - 4);
                ctx.fillStyle = "rgba(0,0,0,.6)";
                ctx.fillRect(x - 2, y - 12, ctx.measureText(label).width + 4, 15);
                ctx.fillStyle = color;
                ctx.fillText(label, x, y);
            });
        }
        // The selected Region field's region, with its corners to resize it.
        const row = selectedRow(card);
        if (row && tf(row, "find").value === "region") {
            const [x1, y1, x2, y2] = rowRegion(row);
            ctx.strokeStyle = COLORS.region;
            ctx.fillStyle = COLORS.region;
            ctx.lineWidth = 1.5;
            ctx.setLineDash([6, 4]);
            ctx.strokeRect(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h);
            ctx.setLineDash([]);
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]].forEach(([x, y]) => ctx.fillRect(x * w - 4, y * h - 4, 8, 8));
        }
    }

    function showResult(card, message) {
        const s = stateOf(card);
        const box = card.querySelector('[data-f="tr-result"]');
        const r = s.last;
        box.className = "tr-result";
        if (message || !r) {
            box.textContent = message || "";
            if (message) box.classList.add("bad");
            return;
        }
        const formats = {};
        rowsOf(card).forEach((row) => { formats[tf(row, "name").value.trim().toLowerCase()] = tf(row, "format").value.trim(); });
        const fields = (r.fields || []).map((f) => (f.text === null || f.text === undefined ? `${f.name}: nothing read`
            : f.fits ? `${f.name}: ${f.text} ✓` : `${f.name}: ${f.text} (expected ${formats[f.name.toLowerCase()] || "its format"})`));
        const verdict = r.error ? `Cannot read: ${r.error}` : r.result === "good" ? "Good" : `Reject: ${REASONS[r.reason] || r.reason}`;
        box.textContent = [verdict].concat(fields, [`${Math.round(r.ms || 0)} ms`]).join(" · ");
        box.classList.add(r.result === "good" ? "good" : "bad");
        if (!card.querySelector(".tr-lines").hidden) showLines(card);
    }

    // Read: every line found, each a button that gives the selected field its format.
    function showLines(card) {
        const s = stateOf(card);
        const box = card.querySelector(".tr-lines");
        const lines = (s.last && s.last.lines) || [];
        box.hidden = false;
        box.innerHTML = lines.length
            ? `<div class="pl-note">Click a line to give the selected field its format.</div>`
                + lines.map((line, i) => `<button data-lock type="button" class="tr-line" data-act="tr-pick" data-line="${i}">${esc(line.text)} <span>${Math.round((line.confidence || 0) * 100)}%</span></button>`).join("")
            : `<div class="pl-note">${s.last ? "No text found in the picture." : "Take a picture first."}</div>`;
    }

    // A preview of the card's settings on the held picture; one at a time, the newest change last.
    async function preview(card) {
        const s = stateOf(card);
        const id = card.dataset.camera;
        if (s.held !== id) return;
        if (s.busy) { s.again = true; return; }
        s.busy = true;
        try {
            s.last = await s.api.fetchJson(`/api/v1/ocr/preview/camera/${encodeURIComponent(id)}`, { method: "POST", body: { settings: s.api.entry(), held: true } });
            showResult(card);
        } catch (e) {
            s.last = null;
            if (/No picture is held/.test(e.message)) s.held = null;
            showResult(card, e.message);
        }
        draw(card);
        s.busy = false;
        if (s.again) { s.again = false; await preview(card); }
    }

    function previewSoon(card) {
        const s = stateOf(card);
        clearTimeout(s.timer);
        s.timer = setTimeout(() => preview(card), PREVIEW_DELAY_MS);
    }

    function showPicture(card, blob) {
        const pic = card.querySelector('[data-f="tr-pic"]');
        const img = pic.querySelector("img");
        return new Promise((resolve) => {
            const url = URL.createObjectURL(blob);
            const previous = img.dataset.blob;
            img.onload = () => {
                pic.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
                img.hidden = false;
                pic.querySelector(".tr-pic-note").textContent = "";
                if (previous) URL.revokeObjectURL(previous);
                resolve();
            };
            img.onerror = () => resolve();
            img.dataset.blob = url;
            img.src = url;
        });
    }

    // Keeps the camera's newest picture on the server, shows it, then reads it.
    async function takePicture(card) {
        const s = stateOf(card);
        const id = card.dataset.camera;
        try {
            const blob = await s.api.fetchBlob(`/api/v1/ocr/picture/camera/${encodeURIComponent(id)}`, { method: "POST" });
            if (card.dataset.camera !== id) return;
            await showPicture(card, blob);
            s.held = id;
        } catch (e) {
            s.held = null;
            s.last = null;
            card.querySelector('[data-f="tr-pic"] .tr-pic-note').textContent = e.message;
            draw(card);
            return;
        }
        await preview(card);
    }

    function setMode(card, mode) {
        const s = stateOf(card);
        s.mode = mode;
        card.querySelector('[data-act="tr-live"]').classList.toggle("active", mode === "live");
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

    // The card's pictures start again where they may: Live while the fold is open on a shown Text reader card.
    function wake(card) {
        const s = stateOf(card);
        if (!s) return;
        if (s.held && s.held !== card.dataset.camera) { s.held = null; s.last = null; setMode(card, "live"); }
        if (!active(card)) return;
        if (s.mode === "live") runLive(card);
        else draw(card);
    }

    // The reader models on this server (GET /api/v1/ocr/models): a card that is not a saved text reader takes the new one.
    function setModels(card, info) {
        const select = card.querySelector('[data-f="ocr_model"]');
        if (!select || !info) return;
        const isNew = card.querySelector(".tr-body").dataset.trNew === "1";
        const current = isNew ? (info.new || "small") : select.value;
        const models = info.models && info.models.length ? info.models : ["small"];
        select.innerHTML = models.map((m) => opt(m, MODEL_LABELS[m] || m, current)).join("")
            + (models.includes(current) ? "" : opt(current, `${MODEL_LABELS[current] || current} (not on this server)`, current));
        refreshSelect(select);
    }

    // The region of the selected Region field, drawn by dragging on the picture, moved from inside it, resized by a corner.
    function bindRegionDrag(card) {
        const pic = card.querySelector('[data-f="tr-pic"]');
        let drag = null;
        const at = (e) => { const r = pic.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width, h: r.height }; };
        pic.addEventListener("pointerdown", (e) => {
            const row = selectedRow(card);
            if (!row || tf(row, "find").value !== "region" || !canEdit(card) || !pic.clientWidth) return;
            const p = at(e);
            const [x1, y1, x2, y2] = rowRegion(row).map((v, i) => v * (i % 2 ? p.h : p.w));
            const corners = [[x1, y1, x2, y2], [x2, y1, x1, y2], [x2, y2, x1, y1], [x1, y2, x2, y1]];
            const corner = corners.find(([cx, cy]) => Math.abs(p.x - cx) <= CORNER_PX && Math.abs(p.y - cy) <= CORNER_PX);
            if (corner) drag = { row, anchor: [corner[2], corner[3]] };
            else if (p.x > x1 && p.x < x2 && p.y > y1 && p.y < y2) drag = { row, from: [p.x, p.y], box: [x1, y1, x2, y2] };
            else drag = { row, anchor: [p.x, p.y] };
            pic.setPointerCapture(e.pointerId);
            e.preventDefault();
        });
        pic.addEventListener("pointermove", (e) => {
            if (!drag) return;
            const p = at(e);
            let region;
            if (drag.anchor) {
                region = regionFrom(drag.anchor[0], drag.anchor[1], p.x, p.y, p.w, p.h);
            } else {
                const [x1, y1, x2, y2] = drag.box;
                const dx = Math.min(Math.max(p.x - drag.from[0], -x1), p.w - x2);
                const dy = Math.min(Math.max(p.y - drag.from[1], -y1), p.h - y2);
                region = regionFrom(x1 + dx, y1 + dy, x2 + dx, y2 + dy, p.w, p.h);
            }
            drag.row.dataset.region = region.join(",");
            drag.moved = true;
            draw(card);
        });
        const end = () => {
            if (drag && drag.moved) changed(card);
            drag = null;
        };
        pic.addEventListener("pointerup", end);
        pic.addEventListener("pointercancel", end);
    }

    // Wires one card. api: fetchJson(path, options), fetchBlob(path, options), entry() (the card's camera entry),
    // active() (the card is a Text reader card on the shown Line setup), yoloModels() and readOnly.
    function bind(card, api) {
        const s = { api, readOnly: Boolean(api.readOnly), mode: "live", held: null, last: null, busy: false, again: false, running: false, timer: null };
        states.set(card, s);
        const body = card.querySelector(".tr-body");
        body.addEventListener("click", (e) => {
            const button = e.target.closest("[data-act]");
            if (!button || !body.contains(button)) return;
            const act = button.dataset.act;
            if (act === "tr-live") { setMode(card, "live"); runLive(card); }
            else if (act === "tr-hold") { setMode(card, "held"); takePicture(card); }
            else if (act === "tr-read") { if (card.querySelector(".tr-lines").hidden) showLines(card); else card.querySelector(".tr-lines").hidden = true; }
            else if (act === "tr-add") { if (addField(card)) changed(card); }
            else if (act === "tr-remove") {
                const row = button.closest(".tr-field");
                const rows = rowsOf(card);
                if (rows.length <= 1) return;
                const index = rows.indexOf(row);
                const fields = rows.map(rowValues).filter((_, i) => i !== index);
                redrawFields(card, fields, Math.min(index, fields.length - 1));
                changed(card);
            } else if (act === "tr-pick") {
                const line = ((s.last && s.last.lines) || [])[Number(button.dataset.line)];
                if (!line) return;
                const row = card.querySelector(".tr-field.selected");
                if (row) tf(row, "format").value = suggestFormat(line.text);
                else if (!addField(card, suggestFormat(line.text))) return;
                changed(card);
            }
        });
        // Typing or a choice in a field row selects that field.
        body.addEventListener("focusin", (e) => {
            const row = e.target.closest(".tr-field");
            if (row && !row.classList.contains("selected")) selectRow(card, row);
        });
        body.addEventListener("pointerdown", (e) => {
            const row = e.target.closest(".tr-field");
            if (row && !row.classList.contains("selected")) selectRow(card, row);
        });
        body.addEventListener("change", (e) => {
            const t = e.target;
            if (t.dataset && t.dataset.f === "ocr_locator") {
                const classes = classesOf(api.yoloModels(), t.value);
                rowsOf(card).forEach((row) => {
                    const select = tf(row, "yolo_class");
                    select.innerHTML = classOptions(classes, classes.includes(select.value) ? select.value : "");
                    refreshSelect(select);
                });
            }
            syncFields(card);
            draw(card);
            previewSoon(card);
        });
        body.addEventListener("input", () => previewSoon(card));
        // The fold opened or closed (production_lines toggles it first).
        card.addEventListener("click", (e) => {
            const head = e.target.closest(".pl-fold-head");
            if (head && head.closest(".pl-fold") === body.closest(".pl-fold")) setTimeout(() => wake(card), 0);
        });
        bindRegionDrag(card);
        setMode(card, "live");
        wake(card);
    }

    const api = { MAX_FIELDS, foldHtml, ocrFromValues, readCard, suggestFormat, regionFrom, bind, wake, setModels };
    root.TextReader = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
