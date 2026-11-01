/*
 * Line setup: Save and Load. Two small text buttons in the Line setup header keep the line's setup, the
 * connections it uses and its cameras' image settings under a name on the server, and put one back.
 *
 * Save opens a small window asking for a name; it saves the setup as it is saved on the server, so it waits
 * while the page has unsaved changes. Load opens a small window listing this line's saves, newest first:
 * a click on one loads it (the line must be stopped), its ✕ deletes it. Saving works while the configuration
 * is locked; loading and deleting carry data-lock.
 *
 * The markup and the checks are plain functions; bind() wires them to the page. Runs in node for
 * tests/test_line_setups_page.py (module.exports), like camera_pages.js.
 */
(function (root) {
    "use strict";

    const MAX_NAME = 64;
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function buttonsHtml(readOnly) {
        if (readOnly) return "";
        return `<button type="button" class="ls-link restricted-l2" data-ls="save">Save</button>`
            + `<button type="button" class="ls-link restricted-l2" data-ls="open-load">Load</button>`;
    }

    function nameProblem(name) {
        const text = String(name ?? "").trim();
        if (!text) return "Give the setup a name.";
        if (text.length > MAX_NAME) return `A name has at most ${MAX_NAME} characters.`;
        return "";
    }

    // The date and time a save was made, in the browser's time.
    function when(iso) {
        const date = new Date(iso);
        if (Number.isNaN(date.getTime())) return String(iso ?? "");
        return date.toLocaleString([], { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    }

    function windowHtml(title, body) {
        return `<div class="modal-card ls-window" role="dialog" aria-modal="true" aria-label="${esc(title)}">
            <div class="ls-head"><span class="modal-title">${esc(title)}</span><button type="button" class="ls-close" data-ls="close" aria-label="Close">✕</button></div>
            ${body}
        </div>`;
    }

    function saveWindowHtml({ lineName, dirty }) {
        return windowHtml("Save line setup", `
            <label class="form-label" for="lsName">Name</label>
            <input type="text" class="form-input" id="lsName" data-ls="name" maxlength="${MAX_NAME}" placeholder="For example Product A" autocomplete="off">
            <div class="pl-note">Saves ${esc(lineName)}'s setup as it is saved, with its connections and camera images.</div>
            ${dirty ? `<div class="ls-problem">Unsaved changes: save the line setup first.</div>` : `<div class="ls-problem" data-ls="problem" hidden></div>`}
            <div class="modal-actions">
                <button type="button" class="btn-action btn-outline" data-ls="close">Cancel</button>
                <button type="button" class="btn-action btn-blue" data-ls="do-save" ${dirty ? "disabled" : ""}>Save</button>
            </div>`);
    }

    function loadWindowHtml({ lineName, saves, running }) {
        const rows = (saves || []).map((s) => `<div class="ls-row">
                <button data-lock type="button" class="ls-pick" data-ls="load" data-id="${esc(s.id)}" ${running ? "disabled" : ""}><b>${esc(s.name)}</b> <span class="ls-line">${esc(lineName)}</span> <span class="ls-date">${esc(when(s.saved_at))}</span></button>
                <button data-lock type="button" class="ls-del" data-ls="delete" data-id="${esc(s.id)}" aria-label="Delete ${esc(s.name)}">✕</button>
            </div>`).join("");
        const note = running ? `<div class="ls-problem">Stop the line to load a setup.</div>` : "";
        const list = rows ? `<div class="ls-list">${rows}</div>` : `<div class="pl-note">No saved setups for this line yet. Use Save to keep one.</div>`;
        return windowHtml("Load line setup", note + list);
    }

    function loadQuestion(name, lineName, dirty) {
        return `Load '${name}' onto ${lineName}? Its setup and camera settings replace the current ones; `
            + `connections it uses that no longer exist are added back.${dirty ? " Unsaved changes are lost." : ""}`;
    }

    // ── On the page ───────────────────────────────────────────────────────────

    let open = null;   // { backdrop, onKey } of the window on screen

    function close() {
        if (!open) return;
        document.removeEventListener("keydown", open.onKey);
        open.backdrop.remove();
        open = null;
    }

    function show(html) {
        close();
        const backdrop = document.createElement("div");
        backdrop.className = "modal-backdrop active ls-backdrop";
        backdrop.innerHTML = html;
        // A confirmation shown over the window closes first with Esc.
        const onKey = (e) => {
            if (e.key === "Escape" && !document.querySelector("#actionModalBackdrop.active")) close();
        };
        backdrop.addEventListener("click", (e) => {
            if (e.target === backdrop || e.target.closest("[data-ls='close']")) close();
        });
        document.addEventListener("keydown", onKey);
        document.body.appendChild(backdrop);
        open = { backdrop, onKey };
        return backdrop;
    }

    function bind(container, opts) {
        const base = () => `/api/v1/lines/${encodeURIComponent(opts.lineId())}/setups`;

        function openSave() {
            const box = show(saveWindowHtml({ lineName: opts.lineName(), dirty: opts.dirty() }));
            const input = box.querySelector("[data-ls='name']");
            const button = box.querySelector("[data-ls='do-save']");
            const problem = box.querySelector("[data-ls='problem']");
            const say = (text) => { if (problem) { problem.textContent = text; problem.hidden = !text; } };
            const save = async () => {
                const name = input.value.trim();
                const wrong = nameProblem(name);
                if (wrong) { say(wrong); input.focus(); return; }
                button.disabled = true;
                try {
                    const saved = await opts.fetchJson(base(), { method: "POST", body: { name } });
                    close();
                    opts.toast(`Saved '${saved.name}'`, "success");
                } catch (e) {
                    say(e.message);
                    button.disabled = false;
                }
            };
            button.addEventListener("click", save);
            input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !button.disabled) save(); });
            setTimeout(() => input.focus(), 30);
        }

        async function openLoad() {
            const lineName = opts.lineName();
            const box = show(windowHtml("Load line setup", `<div class="pl-note">Loading…</div>`));
            let saves;
            try {
                saves = await opts.fetchJson(base());
            } catch (e) {
                box.querySelector(".pl-note").textContent = `Could not list the saves: ${e.message}`;
                return;
            }
            if (!open || open.backdrop !== box) return;     // closed while it loaded
            box.innerHTML = loadWindowHtml({ lineName, saves, running: opts.running() });
            const byId = (id) => saves.find((s) => s.id === id) || { id, name: id };
            box.addEventListener("click", (e) => {
                const pick = e.target.closest("[data-ls='load']");
                const del = e.target.closest("[data-ls='delete']");
                if (pick) {
                    const save = byId(pick.dataset.id);
                    opts.confirm("Load line setup", loadQuestion(save.name, lineName, opts.dirty()), () => load(save));
                } else if (del) {
                    const save = byId(del.dataset.id);
                    opts.confirm("Delete saved setup", `Delete '${save.name}'?`, () => remove(save));
                }
            });
        }

        async function load(save) {
            close();
            try {
                const res = await opts.fetchJson(`${base()}/${encodeURIComponent(save.id)}/load`, { method: "POST" });
                const warnings = (res.warnings || []).concat(res.line && res.line.warning ? [res.line.warning] : []);
                opts.toast(`Loaded '${res.name}'${warnings.length ? `: ${warnings.join("; ")}` : ""}`, warnings.length ? "warning" : "success");
                await opts.afterLoad();
            } catch (e) {
                opts.toast(`Could not load '${save.name}': ${e.message}`, "error");
            }
        }

        async function remove(save) {
            try {
                await opts.fetchJson(`${base()}/${encodeURIComponent(save.id)}`, { method: "DELETE" });
                opts.toast(`Deleted '${save.name}'`, "success");
            } catch (e) {
                opts.toast(`Could not delete '${save.name}': ${e.message}`, "error");
            }
            if (open) openLoad();
        }

        container.addEventListener("click", (e) => {
            const button = e.target.closest("[data-ls='save'], [data-ls='open-load']");
            if (!button || button.disabled) return;
            if (button.dataset.ls === "save") openSave();
            else openLoad();
        });
    }

    const api = { MAX_NAME, buttonsHtml, nameProblem, when, saveWindowHtml, loadWindowHtml, loadQuestion, bind, close };
    root.LineSetups = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
