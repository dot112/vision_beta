/*
 * A camera's image and video settings as a form that renders into any container:
 * resolution and FPS, rotation and flips, the ROI (with drag-to-draw on the
 * whole picture), the USB image controls or the IP transport and buffer, an
 * industrial camera's trigger, exposure, gain and strobe output, and the
 * stream's maximum width and JPEG quality.
 *
 * Used by the camera cards on Line setup (assets/production_lines.js) and by
 * the Settings dialog on the Cameras page for a camera that belongs to no line.
 * read() gives exactly the body of PATCH /api/v1/cameras/{id} {settings: ...}.
 */
(function () {
    "use strict";

    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const pct = (value, fallback) => (value !== null && value !== undefined && value !== "" ? (parseFloat(value) * 100).toFixed(0) : String(fallback));
    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

    function field(name, label, input) {
        return `<div class="form-group" style="margin:0"><label class="form-label">${label}</label>${input.replace("<input", `<input data-cs="${name}"`).replace("<select", `<select data-cs="${name}"`)}</div>`;
    }

    // An industrial camera (GigE Vision, USB3 Vision): how each picture is taken, and its exposure.
    const TRIGGER_NOTES = {
        off: "The camera sends pictures all the time, like a network camera.",
        hardware: "Wire the sensor or the PLC output to the camera's own input: the signal to the input pin, the signal's 0 V to the camera's I/O ground (check the voltage range in the camera's manual). The camera takes one picture per edge. With Count mode \"One picture per trigger\" every picture is one product, and with Read codes \"When an input asks\" every picture is read once. No input is needed.",
        software: "No extra wire. The server sends the camera its trigger command when an input with the action \"Take a picture\", a wire-line crossing (Read codes) or the API asks. It is late by the PLC's read time and the network, so it suits parts that stand still.",
    };

    function industrialHtml(cam) {
        const s = cam.settings || {};
        const mode = ["hardware", "software"].includes(s.trigger_mode) ? s.trigger_mode : "off";
        const edge = s.trigger_edge === "falling" ? "falling" : "rising";
        const opt = (value, label, current) => `<option value="${value}" ${current === value ? "selected" : ""}>${label}</option>`;
        const lists = `cs-lines-${esc(cam.id)}`;
        return `
        <div class="cam-settings-section">Trigger (industrial camera)</div>
        ${field("trigger_mode", "Trigger", `<select class="form-input">${opt("off", "Off: runs freely", mode)}${opt("hardware", "Hardware: the camera's own input", mode)}${opt("software", "Software: the server triggers it", mode)}</select>`)}
        <div class="form-hint cs-trigger-note" style="margin:6px 0 10px"></div>
        <div class="cam-settings-row cs-hardware-only">
            ${field("trigger_line", "Trigger input", `<input type="text" class="form-input" maxlength="32" placeholder="Line0" list="${lists}-in" value="${esc(s.trigger_line || "")}">`)}
            ${field("trigger_edge", "Edge", `<select class="form-input">${opt("rising", "Rising", edge)}${opt("falling", "Falling", edge)}</select>`)}
        </div>
        <div class="cam-settings-row cs-triggered-only">
            ${field("trigger_delay_us", "Trigger delay (µs)", `<input type="number" class="form-input" min="0" max="10000000" placeholder="0" value="${esc(s.trigger_delay_us || "")}">`)}
            <div class="form-hint" style="align-self:end;padding-bottom:8px">From the trigger to the exposure.</div>
        </div>
        <div class="cam-settings-row">
            ${field("exposure_us", "Exposure (µs)", `<input type="number" class="form-input" min="10" max="10000000" placeholder="the camera's own" value="${esc(s.exposure_us ?? "")}">`)}
            ${field("gain_db", "Gain (dB)", `<input type="number" class="form-input" min="0" max="48" step="0.1" placeholder="the camera's own" value="${esc(s.gain_db ?? "")}">`)}
        </div>
        <div class="cam-settings-row">
            ${field("strobe_line", "Strobe output", `<input type="text" class="form-input" maxlength="32" placeholder="none" list="${lists}-out" value="${esc(s.strobe_line || "")}">`)}
            <div class="form-hint" style="align-self:end;padding-bottom:8px">A camera line that is on while the sensor is exposed, for a light. Empty = none.</div>
        </div>
        <div class="cam-settings-row">
            ${field("stream_driver", "Stream driver", `<input type="text" class="form-input" maxlength="32" placeholder="the maker's default" list="${lists}-drv" value="${esc(s.stream_driver || "")}">`)}
            <div class="form-hint" style="align-self:end;padding-bottom:8px">How the pictures reach the server. Empty = the camera maker's default. A camera simulated on this computer needs <b>SocketDriver</b> with pylon (its filter driver never sees it).</div>
        </div>
        <datalist id="${lists}-in"></datalist><datalist id="${lists}-out"></datalist><datalist id="${lists}-drv"></datalist>
        <div class="form-hint cs-camera-state" style="margin:4px 0 10px">Connect the camera to see its inputs and its last picture.</div>
        <div class="cs-trigger-test" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 12px" hidden>
            <button type="button" class="btn-action btn-sm btn-outline restricted-l2 cs-trigger-btn">Software trigger</button>
            <span class="form-hint cs-trigger-result" style="margin:0">One test picture now; nothing is counted.</span>
        </div>
        <div class="pl-warn cs-camera-problems" style="margin:0 0 12px" hidden></div>`;
    }

    function syncIndustrial(root) {
        const select = get(root, "trigger_mode");
        if (!select) return;
        const mode = select.value;
        root.querySelector(".cs-trigger-note").textContent = TRIGGER_NOTES[mode] || "";
        root.querySelector(".cs-hardware-only").hidden = mode !== "hardware";
        root.querySelector(".cs-triggered-only").hidden = mode === "off";
    }

    // What the connected camera says of itself: its inputs, its last picture, the settings it refused.
    async function loadIndustrial(root, cameraId) {
        const state = root.querySelector(".cs-camera-state");
        if (!state) return;
        let p = null;
        try {
            const res = await fetch(`/api/v1/cameras/${encodeURIComponent(cameraId)}/status`, { cache: "no-store" });
            if (res.ok) p = (await res.json()).properties || null;
        } catch (_) { /* shown as not connected */ }
        if (!root.isConnected) return;
        const problems = root.querySelector(".cs-camera-problems");
        if (!p || !p.connected) {
            state.textContent = "Connect the camera to see its inputs and its last picture.";
            problems.hidden = true;
            const test = root.querySelector(".cs-trigger-test");
            if (test) test.hidden = true;
            return;
        }
        const fill = (suffix, names) => {
            const list = root.querySelector(`datalist[id$="${suffix}"]`);
            if (list) list.innerHTML = (names || []).map((n) => `<option value="${esc(n)}"></option>`).join("");
        };
        fill("-in", p.trigger_inputs);
        fill("-out", p.lines);
        fill("-drv", p.stream_drivers);
        const test = root.querySelector(".cs-trigger-test");
        if (test) test.hidden = p.trigger_mode !== "software";
        const label = { off: "Off (runs freely)", hardware: "Hardware", software: "Software" }[p.trigger_mode] || p.trigger_mode;
        const last = p.last_picture_at
            ? `last picture at ${new Date(p.last_picture_at * 1000).toLocaleTimeString()} (${p.pictures} so far)`
            : (p.trigger_mode === "off" ? "no picture yet" : "waiting for a trigger");
        const lost = p.dropped_frames ? ` · ${p.dropped_frames} picture(s) lost in a burst` : "";
        const inputs = (p.trigger_inputs || []).length ? ` · inputs: ${p.trigger_inputs.join(", ")}` : "";
        state.textContent = `${[p.vendor, p.model].filter(Boolean).join(" ") || "Camera"}${p.interface ? ` (${p.interface})` : ""} · trigger now: ${label} · ${last}${lost}${inputs}`;
        const said = [...(p.setting_problems || []), ...(p.picture_error ? [p.picture_error] : [])];
        problems.hidden = !said.length;
        problems.innerHTML = said.length ? `The camera did not take every setting:<br>${said.map((t) => `• ${esc(t)}`).join("<br>")}` : "";
    }

    // One picture by software trigger, to see that the camera answers (POST /cameras/{id}/trigger).
    async function testTrigger(root, cameraId) {
        const button = root.querySelector(".cs-trigger-btn");
        const said = root.querySelector(".cs-trigger-result");
        button.disabled = true;
        said.textContent = "Triggering…";
        try {
            const res = await fetch(`/api/v1/cameras/${encodeURIComponent(cameraId)}/trigger`, { method: "POST" });
            const data = await res.json().catch(() => ({}));
            said.textContent = res.ok ? `Picture #${data.frame_id} came in ${Math.round(data.waited_ms)} ms.`
                : (data.detail || `The trigger failed (HTTP ${res.status}).`);
        } catch (_) {
            said.textContent = "The server did not answer.";
        }
        button.disabled = false;
        loadIndustrial(root, cameraId);
    }

    function bindIndustrial(root, cameraId) {
        if (!get(root, "trigger_mode")) return;
        const triggerButton = root.querySelector(".cs-trigger-btn");
        if (triggerButton) triggerButton.addEventListener("click", () => testTrigger(root, cameraId));
        get(root, "trigger_mode").addEventListener("change", () => syncIndustrial(root));
        syncIndustrial(root);
        loadIndustrial(root, cameraId);
        const timer = setInterval(() => {
            if (!root.isConnected) { clearInterval(timer); return; }
            if (!document.hidden && root.offsetParent !== null) loadIndustrial(root, cameraId);
        }, 3000);
    }

    // The image part: what the camera captures and what goes to the model.
    function imageHtml(cam) {
        const s = cam.settings || {};
        const rotation = String(s.rotation || "");
        const rotOption = (value, label) => `<option value="${value}" ${rotation === value ? "selected" : ""}>${label}</option>`;
        const usb = cam.type === "usb";
        const ip = cam.type === "ip";
        const industrial = cam.type === "gige";
        return `${industrial ? industrialHtml(cam) : ""}
        <div class="cam-settings-section">Resolution</div>
        ${industrial ? `<div class="form-hint" style="margin:-4px 0 10px">Empty = the camera's own size. A size here scales the picture in the server; FPS does not apply to this camera.</div>` : ""}
        <div class="cam-settings-row triple">
            ${field("width", "Width (px)", `<input type="number" class="form-input" placeholder="640" min="80" max="3840" value="${esc(s.width || "")}">`)}
            ${field("height", "Height (px)", `<input type="number" class="form-input" placeholder="480" min="60" max="2160" value="${esc(s.height || "")}">`)}
            ${field("fps", "FPS", `<input type="number" class="form-input" placeholder="25" min="1" max="120" value="${esc(s.fps || "")}">`)}
        </div>
        <div class="cam-settings-section">Rotation &amp; flip</div>
        <div class="cam-settings-row">
            ${field("rotation", "Rotation", `<select class="form-input">${rotOption("", "No rotation")}${rotOption("90", "90° clockwise")}${rotOption("180", "180°")}${rotOption("270", "270° (counter-clockwise)")}</select>`)}
            <div class="form-group" style="margin:0;padding-top:20px;display:flex;gap:16px;align-items:center">
                <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type="checkbox" data-cs="flip_h" ${s.flip_h ? "checked" : ""}> Flip H</label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type="checkbox" data-cs="flip_v" ${s.flip_v ? "checked" : ""}> Flip V</label>
            </div>
        </div>
        <div class="cam-settings-section">Region of interest (ROI / crop)</div>
        <div class="form-hint" style="margin:-4px 0 10px">Only this part of the picture goes to the AI model, the code reader and the live video. Drag on the picture to draw it.</div>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
            <span class="form-label" style="margin:0">Enable ROI</span>
            <label class="switch"><input type="checkbox" data-cs="roi_enabled" ${s.roi_enabled ? "checked" : ""}><span class="slider"></span></label>
            <button type="button" class="btn-action btn-sm btn-outline cs-roi-reset" style="margin-left:auto">Whole picture</button>
        </div>
        <div class="cs-roi-fields">
            <div class="cam-settings-row">
                ${field("roi_x", "X % (left)", `<input type="number" class="form-input" min="0" max="99" value="${pct(s.roi_x, 0)}">`)}
                ${field("roi_y", "Y % (top)", `<input type="number" class="form-input" min="0" max="99" value="${pct(s.roi_y, 0)}">`)}
            </div>
            <div class="cam-settings-row">
                ${field("roi_w", "Width %", `<input type="number" class="form-input" min="1" max="100" value="${pct(s.roi_w, 100)}">`)}
                ${field("roi_h", "Height %", `<input type="number" class="form-input" min="1" max="100" value="${pct(s.roi_h, 100)}">`)}
            </div>
            <div class="roi-preview cs-roi-preview">
                <img class="cs-roi-img" alt="" hidden>
                <div class="roi-box cs-roi-box" style="left:0;top:0;width:100%;height:100%"></div>
                <span class="roi-preview-note cs-roi-note">Crop preview</span>
            </div>
        </div>
        ${usb ? `
        <div class="cam-settings-section">Image controls (USB)</div>
        <div class="cam-settings-row">
            ${field("brightness", "Brightness", `<input type="range" class="form-input" min="-100" max="100" value="${esc(s.brightness ?? 0)}" style="padding:4px">`)}
            ${field("contrast", "Contrast", `<input type="range" class="form-input" min="-100" max="100" value="${esc(s.contrast ?? 0)}" style="padding:4px">`)}
        </div>
        <div class="cam-settings-row">
            ${field("saturation", "Saturation", `<input type="range" class="form-input" min="-100" max="100" value="${esc(s.saturation ?? 0)}" style="padding:4px">`)}
            ${field("exposure", "Exposure", `<input type="range" class="form-input" min="-13" max="0" value="${esc(s.exposure ?? -6)}" style="padding:4px">`)}
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span class="form-label" style="margin:0">Auto exposure</span>
            <label class="switch"><input type="checkbox" data-cs="auto_exposure" ${s.auto_exposure !== false ? "checked" : ""}><span class="slider"></span></label>
        </div>` : ""}
        ${ip ? `
        <div class="cam-settings-section">IP stream</div>
        <div class="cam-settings-row">
            ${field("transport", "Transport protocol", `<select class="form-input"><option value="tcp" ${(s.transport || "tcp") === "tcp" ? "selected" : ""}>TCP (reliable)</option><option value="udp" ${s.transport === "udp" ? "selected" : ""}>UDP (low latency)</option></select>`)}
            ${field("buffer_size", "Buffer size", `<input type="number" class="form-input" min="1" max="10" value="${esc(s.buffer_size || 1)}">`)}
        </div>` : ""}`;
    }

    // The video part: what the dashboards are sent.
    function videoHtml(cam, withTitle) {
        const s = cam.settings || {};
        return `${withTitle ? `<div class="cam-settings-section">Video stream</div>` : ""}
        <div class="cam-settings-row">
            ${field("stream_max_width", "Max stream width (px)", `<input type="number" class="form-input" placeholder="640" min="160" max="3840" value="${esc(s.stream_max_width || 640)}">`)}
            ${field("stream_jpeg_quality", "JPEG quality %", `<input type="number" class="form-input" placeholder="65" min="20" max="95" value="${esc(s.stream_jpeg_quality || 65)}">`)}
        </div>
        <div class="form-hint">Only the live video changes; the AI model always gets the full picture.</div>`;
    }

    const get = (root, name) => root.querySelector(`[data-cs="${name}"]`);

    function roiValues(root) {
        const num = (name, fallback) => parseFloat(get(root, name)?.value) || fallback;
        const x = clamp(num("roi_x", 0), 0, 99);
        const y = clamp(num("roi_y", 0), 0, 99);
        const w = clamp(num("roi_w", 100), 1, 100 - x);
        const h = clamp(num("roi_h", 100), 1, 100 - y);
        return { x, y, w, h };
    }

    function updateROI(root) {
        const box = root.querySelector(".cs-roi-box");
        const on = get(root, "roi_enabled");
        if (!box || !on) return;
        const { x, y, w, h } = roiValues(root);
        root.querySelector(".cs-roi-fields").classList.toggle("roi-off", !on.checked);
        box.hidden = !on.checked;
        Object.assign(box.style, { left: `${x}%`, top: `${y}%`, width: `${w}%`, height: `${h}%` });
    }

    function setROI(root, x, y, w, h) {
        const r = (v) => String(Math.round(v));
        get(root, "roi_x").value = r(x);
        get(root, "roi_y").value = r(y);
        get(root, "roi_w").value = r(w);
        get(root, "roi_h").value = r(h);
        updateROI(root);
    }

    // Drag on the picture to draw the ROI (mouse, pen or touch).
    function bindROI(root) {
        const area = root.querySelector(".cs-roi-preview");
        if (!area) return;
        let start = null;
        const at = (e) => {
            const r = area.getBoundingClientRect();
            return { x: clamp((e.clientX - r.left) / r.width * 100, 0, 100), y: clamp((e.clientY - r.top) / r.height * 100, 0, 100) };
        };
        area.addEventListener("pointerdown", (e) => {
            start = at(e);
            area.setPointerCapture(e.pointerId);
            get(root, "roi_enabled").checked = true;
            e.preventDefault();
        });
        area.addEventListener("pointermove", (e) => {
            if (!start) return;
            const p = at(e);
            setROI(root, Math.min(start.x, p.x), Math.min(start.y, p.y), Math.max(Math.abs(p.x - start.x), 1), Math.max(Math.abs(p.y - start.y), 1));
        });
        const end = () => {
            if (!start) return;
            start = null;
            const { w, h } = roiValues(root);
            if (w < 3 || h < 3) setROI(root, 0, 0, 100, 100);  // a click, not a drag
            root.dispatchEvent(new Event("change", { bubbles: true }));
        };
        area.addEventListener("pointerup", end);
        area.addEventListener("pointercancel", end);
        root.querySelector(".cs-roi-reset")?.addEventListener("click", () => {
            setROI(root, 0, 0, 100, 100);
            root.dispatchEvent(new Event("change", { bubbles: true }));
        });
        root.addEventListener("input", (e) => { if (e.target.matches && e.target.matches('[data-cs^="roi_"]')) updateROI(root); });
        root.addEventListener("change", (e) => { if (e.target.matches && e.target.matches('[data-cs^="roi_"]')) updateROI(root); });
        updateROI(root);
    }

    // The camera's whole picture (before any crop) behind the ROI box.
    async function loadPicture(root, cameraId, stillWanted) {
        const img = root.querySelector(".cs-roi-img");
        const note = root.querySelector(".cs-roi-note");
        const box = root.querySelector(".cs-roi-preview");
        if (!img) return;
        img.hidden = true;
        box.style.aspectRatio = "";
        note.textContent = "Loading the camera picture…";
        try {
            const res = await fetch(`/api/v1/cameras/${encodeURIComponent(cameraId)}/frame?full=true`, { cache: "no-store" });
            if (!res.ok) throw new Error();
            const url = URL.createObjectURL(await res.blob());
            if (stillWanted && !stillWanted()) { URL.revokeObjectURL(url); return; }
            if (img.dataset.blob) URL.revokeObjectURL(img.dataset.blob);
            img.dataset.blob = url;
            img.onload = () => {
                box.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
                img.hidden = false;
                note.textContent = "";
            };
            img.src = url;
        } catch (_) {
            note.textContent = "Connect the camera to draw the ROI on its picture.";
        }
    }

    /**
     * Render the form into ``container``. ``cam`` is {id, type, settings};
     * ``parts`` is a list of "image" and "video". Returns { loadPicture() }.
     */
    function render(container, cam, parts = ["image", "video"]) {
        container.innerHTML = parts.map((part) => (part === "image" ? imageHtml(cam) : videoHtml(cam, parts.length > 1))).join("");
        if (parts.includes("image")) { bindROI(container); bindIndustrial(container, cam.id); }
        return { loadPicture: (stillWanted) => loadPicture(container, cam.id, stillWanted) };
    }

    /** The settings shown in ``root`` (one container or several parts of one card), as PATCH /cameras/{id} wants them. */
    function read(root, type) {
        const value = (name) => get(root, name)?.value;
        const checked = (name) => Boolean(get(root, name)?.checked);
        const settings = {};
        if (get(root, "width")) {
            const { x, y, w, h } = roiValues(root);
            Object.assign(settings, {
                width: parseInt(value("width"), 10) || null,
                height: parseInt(value("height"), 10) || null,
                fps: parseInt(value("fps"), 10) || null,
                rotation: value("rotation") === "" ? 0 : parseInt(value("rotation"), 10),
                flip_h: checked("flip_h"),
                flip_v: checked("flip_v"),
                roi_enabled: checked("roi_enabled"),
                roi_x: x / 100, roi_y: y / 100, roi_w: w / 100, roi_h: h / 100,
            });
            if (type === "usb" && get(root, "brightness")) {
                Object.assign(settings, {
                    brightness: parseFloat(value("brightness")),
                    contrast: parseFloat(value("contrast")),
                    saturation: parseFloat(value("saturation")),
                    exposure: parseFloat(value("exposure")),
                    auto_exposure: checked("auto_exposure"),
                });
            }
            if (type === "ip" && get(root, "transport")) {
                Object.assign(settings, {
                    transport: value("transport"),
                    buffer_size: parseInt(value("buffer_size"), 10) || 1,
                });
            }
        }
        if (type === "gige" && get(root, "trigger_mode")) {
            const text = (name) => String(value(name) ?? "").trim();
            // An empty number is sent as "": the camera then keeps its own exposure or gain.
            const number = (name) => (text(name) === "" ? "" : Number(text(name)));
            Object.assign(settings, {
                trigger_mode: value("trigger_mode"),
                trigger_line: text("trigger_line"),
                trigger_edge: value("trigger_edge"),
                trigger_delay_us: number("trigger_delay_us") || 0,
                exposure_us: number("exposure_us"),
                gain_db: number("gain_db"),
                strobe_line: text("strobe_line"),
                stream_driver: text("stream_driver"),
            });
        }
        if (get(root, "stream_max_width")) {
            settings.stream_jpeg_quality = parseInt(value("stream_jpeg_quality"), 10) || 65;
            settings.stream_max_width = parseInt(value("stream_max_width"), 10) || 640;
        }
        Object.keys(settings).forEach((k) => { if (settings[k] === null) delete settings[k]; });
        return settings;
    }

    window.CameraSettingsForm = { render, read };
})();
