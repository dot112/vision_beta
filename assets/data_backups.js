/*
 * Data and backups (Admin): the disk and its free-space floor, the database, the archive files,
 * the second folder, the clock guard, the backups, and the notices of a restore or of records
 * deleted for space.
 *
 * load() asks GET /api/v1/system/data and draws the page into #dataBackupsPage; dashboard.html
 * calls it when the tab opens. The buttons: OK on a notice, The clock is right, Back up now and
 * Download (a backup as a zip, through fetch so the sign-in header goes with it).
 * Runs in node for tests/test_data_backups_dashboard.py (module.exports).
 */
(function (root) {
    "use strict";

    const CLOCK_REASONS = {
        would_delete_everything: "The newest record is older than the records are kept, so deleting would remove them all.",
        clock_jumped: "The clock jumped forward while the server was running.",
    };
    const NOTICE_TITLES = {
        "storage.records_lost": "Records deleted for space",
        "storage.settings_restored": "Settings restored from a copy",
        "storage.database_restored": "Database rebuilt",
    };
    const KINDS = { nightly: "Nightly", upgrade: "Before an upgrade", manual: "Manual" };

    const esc = (value) => String(value === undefined || value === null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    const known = (value) => value !== null && value !== undefined && Number.isFinite(Number(value));

    function formatBytes(bytes) {
        if (!known(bytes)) return "—";
        let value = Number(bytes);
        if (value < 1024) return `${Math.round(value)} B`;
        const units = ["KB", "MB", "GB", "TB"];
        let unit = -1;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit += 1;
        }
        return `${value.toFixed(1)} ${units[unit]}`;
    }

    function when(iso) {
        if (!iso) return "never";
        const time = new Date(iso);
        return Number.isNaN(time.getTime()) ? String(iso) : time.toLocaleString();
    }

    const day = (iso) => (iso ? String(iso).slice(0, 10) : "—");
    const percentOf = (part, whole) => (whole > 0 ? Math.min(100, Math.max(0, (part / whole) * 100)) : 0);
    const row = (label, value) => `<div class="dbk-row"><span class="dbk-label">${label}</span><span class="dbk-value">${value}</span></div>`;

    function noticesHtml(notices) {
        return (notices || []).map((n) => `<div class="card-panel dbk-notice">
            <div class="dbk-notice-text"><b>${esc(NOTICE_TITLES[n.code] || n.code)}</b> <span class="feed-meta num">${esc(when(n.at))}</span>
                <div>${esc(n.message)}</div></div>
            <button class="btn-action btn-outline" type="button" data-notice="${esc(n.code)}">OK</button>
        </div>`).join("");
    }

    function storageHtml(status) {
        const disk = status.disk || {};
        const database = status.database || {};
        const archive = status.archive || {};
        const copy = status.copy || {};
        const last = status.last || {};
        const used = known(disk.used_percent) ? Number(disk.used_percent) : 0;
        const floor = percentOf(disk.floor || 0, disk.total || 0);
        const level = disk.state === "full" ? "high" : disk.state === "low" ? "warn" : "ok";
        const archiveText = !archive.enabled ? "Off"
            : archive.days ? `${archive.days} day(s), ${formatBytes(archive.size)}, ${day(archive.oldest)} to ${day(archive.newest)}`
                + ` (kept ${archive.retention_days ? `${archive.retention_days} days` : "until space is needed"})`
                : "No archive files yet";
        const copyText = !copy.dir ? "No second folder"
            : `${esc(copy.dir)}: ${copy.ok === false ? "not written (see the alarm)" : copy.ok ? `copied ${esc(when(copy.last))}` : "not copied yet"}`;
        const records = database.oldest ? `${day(database.oldest)} to ${day(database.newest)}` : "None yet";
        return `<div class="card-panel">
            <div class="card-panel-header"><div><span class="card-panel-title">Storage</span>
                <div class="card-panel-sub">The disk that holds the server's data, and what is kept on it.</div></div></div>
            <div class="dbk-bar ${level}" data-used="${used.toFixed(1)}" data-floor="${floor.toFixed(1)}">
                <span style="width:${used.toFixed(1)}%"></span><i style="left:${(100 - floor).toFixed(1)}%" title="Free-space floor"></i>
            </div>
            <div class="dbk-grid">
                ${row("Free", `${formatBytes(disk.free)} of ${formatBytes(disk.total)} (${used.toFixed(0)}% used)`)}
                ${row(`<span data-help="Below this much free space the oldest data is deleted to make room.">Free-space floor</span>`,
                      disk.floor ? formatBytes(disk.floor) : "Off")}
                ${row("Database", `${formatBytes(database.size)}, ${formatBytes(disk.room)} free inside`)}
                ${row("Records kept", esc(records))}
                ${row("Archive", esc(archiveText))}
                ${row("Second folder", copyText)}
                ${row("Last cleanup", esc(when(last.cleanup)))}
                ${row("Last archive", esc(when(last.archive)))}
                ${row("Last backup", esc(when(last.backup)))}
            </div>
        </div>`;
    }

    function clockHtml(clock) {
        clock = clock || {};
        if (!clock.paused) {
            return `<div class="card-panel"><div class="card-panel-header"><div><span class="card-panel-title">Clock</span>
                <div class="card-panel-sub">The clock is fine: old data is deleted by age as set.</div></div></div></div>`;
        }
        return `<div class="card-panel dbk-clock">
            <div class="card-panel-header"><div><span class="card-panel-title">Clock: deleting by age is paused</span>
                <div class="card-panel-sub">${esc(CLOCK_REASONS[clock.reason] || "The clock looks wrong.")}${clock.since ? ` Since ${esc(when(clock.since))}.` : ""}</div></div>
                <div class="card-actions"><button class="btn-action" type="button" id="dbkClockOk"
                    data-help="Lets old data be deleted by age again; until then only the free-space floor deletes data.">The clock is right</button></div>
            </div>
        </div>`;
    }

    function backupsHtml(backups) {
        const rows = (backups || []).map((b) => `<tr>
            <td data-label="Made" class="num">${esc(when(b.made_at))}</td>
            <td data-label="Kind">${esc(KINDS[b.kind] || b.kind)}</td>
            <td data-label="Name" class="num">${esc(b.name)}</td>
            <td data-label="Size" class="num">${esc(formatBytes(b.size))}</td>
            <td><button class="btn-action btn-outline" type="button" data-backup="${esc(b.name)}">Download</button></td>
        </tr>`).join("") || `<tr><td colspan="5" class="empty-state">No backups yet. The first is made tonight, or now with Back up now.</td></tr>`;
        return `<div class="card-panel">
            <div class="card-panel-header"><div><span class="card-panel-title">Backups</span>
                <div class="card-panel-sub">The settings and every table but the records; the archive files keep the records.</div></div>
                <div class="card-actions"><button class="btn-action" type="button" id="dbkBackupNow">Back up now</button></div>
            </div>
            <div class="table-responsive"><table class="pl-table table-mobile-cards">
                <thead><tr><th>Made</th><th>Kind</th><th>Name</th><th>Size</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table></div>
        </div>`;
    }

    function pageHtml(status) {
        status = status || {};
        return `<div class="dbk-page">${noticesHtml(status.notices)}${storageHtml(status)}${clockHtml(status.clock)}${backupsHtml(status.backups)}
            <div class="pl-note" id="dbkMessage" role="status"></div></div>`;
    }

    // ── In the browser ────────────────────────────────────────────────────────

    const byId = (id) => root.document && root.document.getElementById(id);

    function say(text) {
        const box = byId("dbkMessage");
        if (box) box.textContent = text || "";
    }

    async function call(path, options) {
        const res = await root.fetch(path, { cache: "no-store", ...(options || {}) });
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON */ }
            throw new Error(detail);
        }
        return res;
    }

    async function download(name) {
        const res = await call(`/api/v1/system/backups/${encodeURIComponent(name)}`);
        const url = root.URL.createObjectURL(await res.blob());
        const a = root.document.createElement("a");
        a.href = url;
        a.download = `${name}.zip`;
        root.document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => root.URL.revokeObjectURL(url), 10000);
    }

    async function act(button, work, busyText) {
        const label = button.textContent;
        button.disabled = true;
        if (busyText) button.textContent = busyText;
        try {
            await work();
            return true;
        } catch (e) {
            say(e.message);
            return false;
        } finally {
            button.disabled = false;
            button.textContent = label;
        }
    }

    async function onClick(event) {
        const button = event.target.closest && event.target.closest("button");
        if (!button) return;
        if (button.dataset.notice) {
            if (await act(button, () => call(`/api/v1/system/data/notices/${encodeURIComponent(button.dataset.notice)}/ok`, { method: "POST" }))) load();
        } else if (button.dataset.backup) {
            await act(button, () => download(button.dataset.backup), "Preparing…");
        } else if (button.id === "dbkBackupNow") {
            if (await act(button, () => call("/api/v1/system/backups", { method: "POST" }), "Backing up…")) load();
        } else if (button.id === "dbkClockOk") {
            if (await act(button, () => call("/api/v1/system/data/clock-ok", { method: "POST" }))) load();
        }
    }

    async function load() {
        const box = byId("dataBackupsPage");
        if (!box) return;
        if (!box.dataset.bound) {
            box.dataset.bound = "1";
            box.addEventListener("click", onClick);
        }
        try {
            const status = await (await call("/api/v1/system/data")).json();
            box.innerHTML = pageHtml(status);
        } catch (e) {
            box.innerHTML = `<div class="card-panel"><div class="empty-state">Could not read the data status: ${esc(e.message)}</div></div>`;
        }
    }

    const api = { load, pageHtml, formatBytes, CLOCK_REASONS };
    root.DataBackups = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
