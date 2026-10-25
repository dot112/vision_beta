/*
 * Configuration lock, dashboard side. The server refuses every locked change (423, see
 * app/security/config_lock.py); this script shows the state and keeps people from editing in vain:
 *   - the lock chip in the header and its dialog: lock (Level 2 and Admin), unlock (Admin,
 *     or Level 2 with the lock code);
 *   - body.config-locked while locked: a banner on the locked pages (dashboard.css, by body[data-tab]);
 *   - the fields of the locked pages and their dialogs are disabled (data-lock-off marks the ones
 *     disabled here, so unlocking enables only those; data-lock-free keeps a field usable);
 *   - buttons marked data-lock do nothing but say that the configuration is locked;
 *   - My account: the Admin's lock code.
 * The state comes from GET /system/lock, from every poll-changes answer and from any 423 answer.
 */
(() => {
    "use strict";

    const LOCKED_MESSAGE = "The configuration is locked. An Admin, or a Level 2 user with the lock code, can unlock it.";
    // The locked pages, the card dialogs opened from them, and dialogs marked data-lock-root.
    const LOCKED_ROOTS = "#tabLines, #tabActionTrigger, #tabCameras, #tabModels, #tabComms, #tabProducts, "
        + "#sendActionSettingsModal, #plcActionSettingsModal, #plcSignalSettingsModal, #commModalOverlay, "
        + "#modelUploadOverlay, [data-lock-root]";
    const FIELDS = "input, select, textarea";
    const WARN_EVERY_MS = 3000;

    // level: this tab's user's level, as the server answered it (see level()).
    const state = { status: null, level: null, warnedAt: 0, observer: null, scheduled: false };
    const byId = (id) => document.getElementById(id);
    const locked = () => Boolean(state.status && state.status.locked);

    // This tab's level. The page keeps its sign-in per tab but the level in storage shared by
    // every tab of the browser: another account signed in on another tab changes the stored one.
    // So the server's answer to this tab's sign-in counts; the stored level only until it comes.
    function level() {
        return state.level || parseInt(localStorage.getItem("clearance_level") || "1", 10) || 1;
    }

    function toast(msg, kind) {
        if (typeof showToast === "function") showToast(msg, kind || "info");
    }

    async function call(method, path, body) {
        const res = await fetch(`/api/v1/system/${path}`, {
            method,
            headers: body === undefined ? {} : { "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        let data = null;
        try { data = await res.json(); } catch (_) { /* no body */ }
        if (!res.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `Error ${res.status}`);
        return data;
    }

    function when(iso) {
        if (!iso) return "";
        const at = new Date(iso);
        return at.toDateString() === new Date().toDateString() ? at.toLocaleTimeString() : at.toLocaleString();
    }

    // ── Fields and buttons of the locked pages ────────────────────────────────

    function lockFields() {
        state.scheduled = false;
        if (!locked()) return;
        document.querySelectorAll(LOCKED_ROOTS).forEach((root) => {
            root.querySelectorAll(FIELDS).forEach((field) => {
                if (field.disabled || field.closest("[data-lock-free]")) return;
                field.disabled = true;
                field.dataset.lockOff = "1";
            });
        });
    }

    function unlockFields() {
        document.querySelectorAll("[data-lock-off]").forEach((field) => {
            field.disabled = false;
            delete field.dataset.lockOff;
        });
    }

    // Pages and dialogs draw their fields again as they change: those are locked too.
    function watch(on) {
        if (on && !state.observer) {
            state.observer = new MutationObserver(() => {
                if (state.scheduled) return;
                state.scheduled = true;
                requestAnimationFrame(lockFields);
            });
            state.observer.observe(document.body, { childList: true, subtree: true });
        } else if (!on && state.observer) {
            state.observer.disconnect();
            state.observer = null;
        }
    }

    function warnLocked() {
        const now = Date.now();
        if (now - state.warnedAt < WARN_EVERY_MS) return;
        state.warnedAt = now;
        showToast(LOCKED_MESSAGE, "warning");
    }

    document.addEventListener("click", (e) => {
        if (!locked() || !e.target.closest || !e.target.closest("[data-lock]")) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        warnLocked();
    }, true);

    // ── Showing the state ─────────────────────────────────────────────────────

    function renderChip() {
        const chip = byId("configLockChip");
        if (!chip) return;
        chip.hidden = !state.status;
        chip.classList.toggle("locked", locked());
        byId("configLockText").textContent = locked() ? "Locked" : "Unlocked";
        chip.title = locked() ? "The configuration is locked" : "The configuration is unlocked";
    }

    function renderBanner() {
        const text = byId("configLockBannerText");
        if (!text) return;
        const s = state.status || {};
        text.textContent = locked()
            ? `Locked${s.changed_by ? ` by ${s.changed_by}` : ""}${s.changed_at ? ` at ${when(s.changed_at)}` : ""}: settings can be viewed but not changed.`
            : "";
        byId("configLockBannerUnlock").hidden = level() < 2;
    }

    function renderCodePanel() {
        const panel = byId("configLockCodePanel");
        if (!panel) return;
        panel.hidden = level() < 3 || !state.status;
        const codeSet = Boolean(state.status && state.status.code_set);
        const label = byId("lockCodeState");
        label.textContent = codeSet ? "Code set" : "No code: only an Admin can unlock";
        label.className = `state-text ${codeSet ? "ok" : ""}`;
        byId("lockCodeRemove").disabled = !codeSet;
    }

    function renderDialog() {
        const dialog = byId("configLockDialog");
        if (!dialog) return;
        const s = state.status || {};
        const lvl = level();
        const message = byId("configLockState");
        const action = byId("configLockAction");
        const codeRow = byId("configLockCodeRow");
        if (locked()) {
            message.textContent = `The configuration is locked${s.changed_by ? ` by ${s.changed_by}` : ""}${s.changed_at ? ` since ${when(s.changed_at)}` : ""}. `
                + (lvl >= 3 ? "Unlock it to change lines, cameras, connections, models or product lists."
                    : lvl === 2 ? (s.code_set ? "Type the lock code to unlock it." : "No lock code is set: only an Admin can unlock.")
                        : "Only an Admin, or a Level 2 user with the lock code, can unlock it.");
            action.textContent = "Unlock";
            action.hidden = lvl < 2 || (lvl === 2 && !s.code_set);
            codeRow.hidden = lvl !== 2 || !s.code_set;
            action.disabled = lvl === 2 && Boolean(s.retry_after);
        } else {
            message.textContent = "The configuration is unlocked: lines, cameras, connections, models and product lists can be changed. "
                + (lvl >= 2 ? "Lock it so that nobody can change them." : "A Level 2 user or an Admin can lock it.");
            action.textContent = "Lock";
            action.hidden = lvl < 2;
            codeRow.hidden = true;
            action.disabled = false;
        }
        if (s.retry_after && locked() && lvl === 2) showError(`Too many wrong codes. Try again in ${s.retry_after} s.`);
    }

    function showError(text) {
        const error = byId("configLockError");
        if (error) error.textContent = text || "";
    }

    function apply(status) {
        if (!status || typeof status !== "object") return;
        // The lock routes say whose answer it is; the poll's copy of the state does not.
        if (Number.isInteger(status.level)) state.level = status.level;
        state.status = status;
        document.body.classList.toggle("config-locked", locked());
        if (locked()) {
            lockFields();
            watch(true);
        } else {
            watch(false);
            unlockFields();
        }
        renderChip();
        renderBanner();
        renderCodePanel();
        if (byId("configLockDialog")?.classList.contains("active")) renderDialog();
    }

    async function refresh() {
        try { apply(await call("GET", "lock")); } catch (_) { /* the next poll brings it */ }
    }

    // Any request answered 423: the configuration was locked meanwhile.
    function onLockedAnswer() {
        if (!locked()) apply({ ...(state.status || {}), locked: true });
        refresh();
        warnLocked();
    }

    // ── Dialog and code panel ─────────────────────────────────────────────────

    function openDialog() {
        showError("");
        const code = byId("configLockCode");
        if (code) code.value = "";
        renderDialog();
        byId("configLockDialog").classList.add("active");
        if (code && !byId("configLockCodeRow").hidden) setTimeout(() => code.focus(), 30);
        refresh();
    }

    function closeDialog() {
        byId("configLockDialog")?.classList.remove("active");
    }

    async function act() {
        const action = byId("configLockAction");
        action.disabled = true;
        showError("");
        try {
            if (locked()) {
                const code = byId("configLockCode").value;
                apply(await call("POST", "unlock", level() >= 3 ? {} : { code }));
                toast("Configuration unlocked", "success");
            } else {
                apply(await call("POST", "lock"));
                toast("Configuration locked", "success");
            }
            closeDialog();
        } catch (err) {
            showError(err.message);
            refresh();
        } finally {
            action.disabled = false;
        }
    }

    async function saveCode() {
        const code = byId("lockCodeNew").value.trim();
        const confirm = byId("lockCodeConfirm").value.trim();
        if (code.length < 6) { toast("The lock code needs at least 6 characters.", "warning"); return; }
        if (code !== confirm) { toast("The two lock codes are not the same.", "warning"); return; }
        try {
            apply(await call("PUT", "lock/code", { code }));
            byId("lockCodeNew").value = "";
            byId("lockCodeConfirm").value = "";
            toast("Lock code saved", "success");
        } catch (err) {
            toast(`Could not save the lock code: ${err.message}`, "warning");
        }
    }

    async function removeCode() {
        try {
            apply(await call("DELETE", "lock/code"));
            toast("Lock code removed: only an Admin can unlock now", "success");
        } catch (err) {
            toast(`Could not remove the lock code: ${err.message}`, "warning");
        }
    }

    byId("configLockCode")?.addEventListener("keydown", (e) => { if (e.key === "Enter") act(); });

    window.ConfigLock = { apply, refresh, locked, openDialog, closeDialog, act, onLockedAnswer, saveCode, removeCode, LOCKED_MESSAGE };
})();
