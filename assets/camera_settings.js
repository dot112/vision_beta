/*
 * A camera's image and video settings as a form that renders into any container:
 * resolution and FPS, rotation and flips, the ROI (with drag-to-draw on the
 * whole picture), the USB image controls or the IP transport and buffer, and
 * the stream's maximum width and JPEG quality.
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

    // The image part: what the camera captures and what goes to the model.
    function imageHtml(cam) {
        const s = cam.settings || {};
        const rotation = String(s.rotation || "");
        const rotOption = (value, label) => `<option value="${value}" ${rotation === value ? "selected" : ""}>${label}</option>`;
        const usb = cam.type === "usb";
        const ip = cam.type === "ip";
        return `
        <div class="cam-settings-section">Resolution</div>
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
        if (parts.includes("image")) bindROI(container);
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
        if (get(root, "stream_max_width")) {
            settings.stream_jpeg_quality = parseInt(value("stream_jpeg_quality"), 10) || 65;
            settings.stream_max_width = parseInt(value("stream_max_width"), 10) || 640;
        }
        Object.keys(settings).forEach((k) => { if (settings[k] === null) delete settings[k]; });
        return settings;
    }

    window.CameraSettingsForm = { render, read };
})();
