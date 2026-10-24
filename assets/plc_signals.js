/*
 * PLC signals (inputs) for the Line setup page of the dashboard (/dashboard).
 *
 * A signal card reads one PLC address and lets the PLC act on the line:
 * start or stop it, count products, take a picture, set the batch number,
 * confirm a PLC action or raise an alarm. The cards are saved with the line
 * (PUT /api/v1/lines/{id} with plc_inputs). The server reads the addresses
 * and reports each card's value, last change and error, which the cards show
 * while Line setup is open.
 *
 * Loaded before assets/production_lines.js, which calls PlcSignals.load() for
 * the line it selects. It uses the dashboard's own helpers (showToast,
 * allCommsEndpoints, applyRoleRestrictions) and the PLC action card styles of
 * assets/dashboard.css.
 */
(function () {
    "use strict";

    const PRIMARY = "line-1";
    const MAX_SIGNALS = 32;        // line_config.MAX_PLC_INPUTS
    const DEFAULT_POLL_MS = 100;   // line_config.DEFAULT_PLC_INPUT_POLL_MS
    const VIEW_KEY = "plc_signals_view";
    const STATUS_MS = 1000;

    // What a signal can do, in the order the dialog lists them (line_config.PLC_INPUT_ACTIONS).
    // edge: the action runs when the value changes the chosen way; the others follow the value itself.
    const ACTIONS = [
        { id: "start_line", label: "Start the line", edge: true,
          note: "Starts the line when the signal comes. A bit that is already on when the server starts does not start the line." },
        { id: "stop_line", label: "Stop the line", edge: true,
          note: "Stops the line when the signal comes." },
        { id: "run_level", label: "Run the line while on", edge: false,
          note: "The line runs while the value is on and stops when it goes off. The value at the first read changes nothing." },
        { id: "reset_counters", label: "Reset the counters", edge: true,
          note: "Resets the line's counts when the signal comes." },
        { id: "product_sensor", label: "Product sensor", edge: true,
          note: "Each signal is one product, for a line without a vision camera. With a code reader and Sync, the product is paired with its code." },
        { id: "capture", label: "Take a picture", edge: true,
          note: "One picture with the camera below: a code reader reads its codes, a vision camera inspects one product." },
        { id: "set_batch", label: "Set the batch number", edge: false,
          note: "The value becomes the line's batch number, at the first read and each time it changes. At most 64 characters." },
        { id: "confirm_card", label: "Confirm an output action", edge: true,
          note: "Tells the line that an output action really happened, for example a sensor that sees the rejected product leave the belt." },
        { id: "alarm", label: "Raise an alarm", edge: false,
          note: "An alarm is active while the value is on and clears when it goes off. Output actions and send cards with the Alarm trigger can react to it." },
        { id: "none", label: "Only fires cards", edge: false,
          note: "The line does nothing itself. Output actions and send cards with the trigger Input changed react to this input, and its value name gives its value to formulas and rules." },
    ];
    const actionOf = (id) => ACTIONS.find((a) => a.id === id) || ACTIONS[0];

    // How the value is read (line_config.PLC_INPUT_DATA_TYPES). only: the actions that may use the type.
    const DATA_TYPES = [
        { id: "auto", label: "Auto (the address's own type)" },
        { id: "bool", label: "Bit (on / off)" },
        { id: "int16", label: "INT16 (-32768 to 32767)" },
        { id: "uint16", label: "UINT16 (0 to 65535)" },
        { id: "int32", label: "INT32 (two words)" },
        { id: "real", label: "REAL (32-bit float)" },
        { id: "string", label: "Text (string)", only: ["set_batch", "none"] },
        { id: "counter", label: "PLC counter (16-bit, counts up)", only: ["product_sensor"] },
    ];
    const EDGES = [
        { id: "rising", label: "Off → on (rising)", short: "Off → on" },
        { id: "falling", label: "On → off (falling)", short: "On → off" },
        { id: "change", label: "Any change", short: "Any change" },
    ];
    const SEVERITIES = [
        { id: "info", label: "Info" },
        { id: "warning", label: "Warning" },
        { id: "critical", label: "Critical" },
    ];

    // PLC channel protocols (PLCDriverFactory): label, an example for the address box, how its addresses are written.
    const MODBUS_HINT = "C5 is a coil, 10001 a discrete input, R10 or 40011 a holding register, 30001 an input register. R10:DINT and R10:REAL read two registers.";
    const PROTOCOLS = {
        modbus_tcp: ["Modbus TCP", "C5, 10001, R10, 30001", MODBUS_HINT],
        modbus_rtu: ["Modbus RTU", "C5, 10001, R10, 30001", MODBUS_HINT],
        s7: ["Siemens S7", "I0.0, M10.4, MW10", "A bit (I0.0, M10.4, DB1.DBX0.0), a byte (MB3), a word (MW10) or a double word (MD8, DB1.DBD8)."],
        eip: ["EtherNet/IP", "Line1_Start", "The tag's name. It is read as the tag's own type."],
        opcua: ["OPC UA", "Tag_1", "The tag's name as the PLC shows it (Tag_1, DB1.Start); the server finds its full NodeId. A full NodeId also works."],
        melsec: ["Mitsubishi MELSEC", "M5, D100", "A bit (M5) or a word (D100). D100:DINT and D100:REAL read two words."],
        fins: ["Omron FINS", "CIO 0.05, W10.01, D100", "A bit (CIO 0.05, W10.01) or a word (D100). D100:DINT and D100:REAL read two words."],
    };
    const PROTOCOL_ALIASES = {
        modbus: "modbus_tcp", rtu: "modbus_rtu", siemens_s7: "s7", siemens: "s7", ethernet_ip: "eip", ethernetip: "eip", ab: "eip",
        opc_ua: "opcua", slmp: "melsec", mc_protocol: "melsec", mitsubishi: "melsec", omron_fins: "fins", omron: "fins",
    };
    // The Generic TCP driver only sends: it has no read.
    const UNREADABLE = new Set(["generic_tcp", "generic", "tcp"]);

    const state = {
        lineId: null,
        cards: [],                 // the cards as edited on this page
        applied: null,             // signature of the cards the server has; null until they are loaded
        savedActions: new Map(),   // card id -> action, as the server has them
        cameras: [],               // the line's cameras as saved (for "Take a picture" and "Product sensor")
        loadError: "",
        inFlight: false,
        status: {},                // card id -> the server's status row
        running: true,             // false: the server is not reading inputs at all
        statusBusy: false,
        timer: null,
        draft: null,               // the card open in the dialog
        draftIsNew: false,         // the draft is a card Add made: OK places it, Cancel drops it
    };

    // ── Small helpers ─────────────────────────────────────────────────────────

    const esc = (value) => String(value === null || value === undefined ? "" : value)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const byId = (id) => document.getElementById(id);
    const toast = (msg, kind) => { if (typeof showToast === "function") showToast(msg, kind || "info"); };
    const selectedLine = () => (window.ProductionLines && window.ProductionLines.lineId) || PRIMARY;
    const supervisor = () => (typeof canConfigure === "function" ? canConfigure() : true);
    const signedIn = () => { try { return Boolean(currentAuthToken); } catch (_) { return false; } };
    const lineUrl = (rest = "") => `/api/v1/lines/${encodeURIComponent(state.lineId || selectedLine())}${rest}`;
    const clamp = (value, low, high, fallback) => {
        const n = parseInt(value, 10);
        return Number.isFinite(n) ? Math.min(high, Math.max(low, n)) : fallback;
    };

    async function api(path, options = {}) {
        const opts = { ...options, headers: { ...(options.headers || {}) } };
        if (opts.body && typeof opts.body !== "string") {
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

    function plcChannels() {
        let all = [];
        try { all = Array.isArray(allCommsEndpoints) ? allCommsEndpoints : []; } catch (_) { all = []; }
        return all.filter((e) => String(e.protocol || "").toLowerCase() === "plc");
    }
    const channelOf = (card) => plcChannels().find((e) => e.id === card.plc_endpoint_id) || null;
    function protocolKey(channel) {
        const raw = String((channel && (channel.plc_protocol || channel.plc_sub_protocol)) || "modbus_tcp").toLowerCase();
        return PROTOCOL_ALIASES[raw] || raw;
    }
    const unreadable = (channel) => Boolean(channel) && UNREADABLE.has(protocolKey(channel));
    const protocolLabel = (channel) => (unreadable(channel) ? "Generic TCP" : (PROTOCOLS[protocolKey(channel)] || [protocolKey(channel).toUpperCase()])[0]);

    function cameraName(id) {
        let known = [];
        try { known = Array.isArray(_allCamsData) ? _allCamsData : []; } catch (_) { known = []; }
        const cam = known.find((c) => c.id === id);
        return cam ? cam.name : id;
    }
    const readsCodes = (cam) => cam.role === "qr" || cam.read_codes === true;
    // A camera a signal can take a picture with (line_config._check_signal_cameras).
    const takesSignal = (cam) => cam.count_mode === "trigger" || (readsCodes(cam) && cam.qr_trigger === "signal");
    const lineHasVision = () => state.cameras.some((cam) => cam.role !== "qr");

    // The PLC actions (as edited on this page) that name a signal as their confirmation.
    function actionsNaming(id) {
        let cards = [];
        try { cards = Array.isArray(_plcActionCards) ? _plcActionCards : []; } catch (_) { cards = []; }
        return cards.filter((c) => c.confirm_input_id === id);
    }

    function newId() {
        const bytes = new Uint8Array(4);
        (window.crypto || window.msCrypto).getRandomValues(bytes);
        return "plcin_" + [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
    }

    // A card as it is sent and compared: the fields of its own action only.
    function cleanCard(card) {
        const action = actionOf(card.action);
        const out = {
            id: card.id,
            name: String(card.name || "").trim(),
            enabled: card.enabled !== false,
            plc_endpoint_id: card.plc_endpoint_id || "",
            address: String(card.address || "").trim(),
            data_type: card.data_type || "auto",
            poll_ms: clamp(card.poll_ms, 50, 10000, DEFAULT_POLL_MS),
            edge: card.edge || "rising",
            debounce_ms: clamp(card.debounce_ms, 0, 5000, 0),
            action: action.id,
        };
        const valueName = String(card.value_name || "").trim();
        if (valueName && card.data_type !== "counter") out.value_name = valueName;
        if (action.id === "capture") {
            out.trigger_no = clamp(card.trigger_no, 1, MAX_TRIGGER_NO, nextTriggerNo(card.id));
            // A card saved before cameras chose their triggers still names its camera; the server turns that into a tick.
            const named = [...new Set([...(card.camera_ids || []), card.camera_id].filter(Boolean))];
            if (named.length) out.camera_ids = named;
        }
        if (action.id === "alarm") {
            out.alarm_text = String(card.alarm_text || "").trim();
            out.alarm_severity = card.alarm_severity || "warning";
        }
        return out;
    }
    const signature = (cards) => JSON.stringify(cards.map(cleanCard));
    const hasPending = () => (state.applied === null ? state.cards.length > 0 : signature(state.cards) !== state.applied);

    // ── Trigger numbers ────────────────────────────────────────────
    // A "Take a picture" input has a trigger number. A camera ticks, on its own card, the
    // triggers that take its picture (assets/production_lines.js reads them with triggers()).

    const MAX_TRIGGER_NO = 99;
    const takesPictures = (card) => card.action === "capture";

    function nextTriggerNo(exceptId) {
        const used = new Set(state.cards.filter((c) => c.id !== exceptId && takesPictures(c)).map((c) => Number(c.trigger_no)));
        let number = 1;
        while (used.has(number)) number += 1;
        return number;
    }

    // An input saved before trigger numbers existed gets one here, as the server gives it on the next save.
    function numberTriggers(cards) {
        cards.filter(takesPictures).forEach((card) => {
            if (!(Number(card.trigger_no) >= 1)) card.trigger_no = nextTriggerNo(card.id);
        });
    }

    // The line's triggers as edited on this page: [{no, name, enabled, cameras}]. ``cameras``: the
    // camera ids an older save still names on the input itself.
    function triggers() {
        return state.cards.filter(takesPictures).map((card) => ({
            no: Number(card.trigger_no), name: card.name || "", enabled: card.enabled !== false,
            cameras: [...new Set([...(card.camera_ids || []), card.camera_id].filter(Boolean))],
        })).filter((t) => t.no >= 1).sort((a, b) => a.no - b.no);
    }

    // The names of the line's cameras (as saved) that have this trigger ticked.
    function camerasOn(card) {
        const number = Number(card.trigger_no);
        const named = new Set([...(card.camera_ids || []), card.camera_id].filter(Boolean));
        return state.cameras.filter((cam) => (cam.triggers || []).includes(number) || named.has(cam.camera_id))
            .map((cam) => cameraName(cam.camera_id));
    }

    // ── Load, apply ────────────────────────────────────────────────

    function setFromServer(line) {
        const cards = Array.isArray(line.plc_inputs) ? line.plc_inputs : [];
        state.cards = cards.map((card) => ({ ...card }));
        numberTriggers(state.cards);
        state.applied = signature(state.cards);
        state.savedActions = new Map(cards.map((card) => [card.id, card.action]));
        state.cameras = Array.isArray(line.cameras) ? line.cameras : [];
        state.loadError = "";
    }

    async function load(lineId) {
        const wanted = lineId || selectedLine();
        state.lineId = wanted;
        closeDialog();
        state.status = {};
        try {
            const line = await api(lineUrl());
            if (wanted !== state.lineId) return;        // another line was chosen meanwhile
            setFromServer(line);
        } catch (e) {
            if (wanted !== state.lineId) return;
            state.cards = [];
            state.applied = null;
            state.savedActions = new Map();
            state.loadError = e.message;
        }
        render();
        if (!state.timer) state.timer = setInterval(refreshStatus, STATUS_MS);
        refreshStatus();
    }

    async function apply() {
        if (state.inFlight) return false;
        if (!signedIn() || !supervisor()) {
            toast("Sign in with a supervisor account to apply inputs.", "warning");
            return false;
        }
        const wanted = state.lineId || selectedLine();
        const snapshot = state.cards.map(cleanCard);
        const sent = JSON.stringify(snapshot);
        state.inFlight = true;
        updateSaveState();
        try {
            const line = await api(lineUrl(), { method: "PUT", body: { plc_inputs: snapshot } });
            if (wanted !== state.lineId) return true;
            const edited = signature(state.cards) !== sent;     // edits made while the request ran
            const working = state.cards;
            setFromServer(line);
            if (edited) state.cards = working;
            toast(line.warning || (edited ? "Inputs applied; newer edits are still unsaved." : "Inputs applied to the server."),
                line.warning ? "warning" : "success");
            render();
            refreshStatus();
            // Line setup shows the line's warnings (a reader that waits for a signal no card sends).
            if (window.ProductionLines && window.ProductionLines.refreshDetail) window.ProductionLines.refreshDetail().catch(() => {});
            return true;
        } catch (e) {
            toast(`Inputs were not applied: ${e.message}`, "warning");
            return false;
        } finally {
            state.inFlight = false;
            updateSaveState();
        }
    }

    // The line as the pages around this one load it: its cameras, and cards changed elsewhere.
    window.addEventListener("pl:detail", (e) => {
        const line = e.detail;
        if (!line || line.id !== state.lineId) return;
        state.cameras = Array.isArray(line.cameras) ? line.cameras : [];
        if (state.inFlight || state.draft || state.applied === null || hasPending()) return;
        if (signature(Array.isArray(line.plc_inputs) ? line.plc_inputs : []) === state.applied) return;
        setFromServer(line);
        render();
    });

    // ── Cards and list ────────────────────────────────────────────────────────

    function view() {
        try { return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "cards"; } catch (_) { return "cards"; }
    }
    function setView(value) {
        try { localStorage.setItem(VIEW_KEY, value === "list" ? "list" : "cards"); } catch (_) { /* not saved */ }
        render();
    }

    function updateSaveState() {
        const label = byId("plcSignalSaveState");
        if (!label) return;
        const dirty = hasPending();
        label.textContent = state.inFlight ? "Applying to server…"
            : state.loadError ? "Not loaded"
                : (dirty ? "Unsaved changes" : "Applied to server");
        label.style.color = dirty || state.loadError ? "var(--warning-color, #f59e0b)" : "var(--text-muted)";
    }

    // When a card acts, in a few words.
    function whenText(card) {
        const action = actionOf(card.action);
        if (card.data_type === "counter") return "The counter goes up";
        if (action.id === "run_level") return "On: run · off: stop";
        if (action.id === "alarm") return "While the value is on";
        if (action.id === "set_batch") return "The value changes";
        return (EDGES.find((e) => e.id === card.edge) || EDGES[0]).short;
    }

    function summary(card) {
        const channel = channelOf(card);
        return {
            channelName: channel ? (channel.name || "PLC") : "No channel chosen",
            protocol: channel ? protocolLabel(channel) : "NONE",
            does: actionOf(card.action).label + (takesPictures(card) && card.trigger_no ? ` · Trigger ${card.trigger_no}` : ""),
            when: whenText(card),
            address: card.address || "—",
            every: `${clamp(card.poll_ms, 50, 10000, DEFAULT_POLL_MS)} ms`,
        };
    }

    function valueText(value) {
        if (value === null || value === undefined) return "—";
        if (value === true) return "On";
        if (value === false) return "Off";
        return String(value);
    }
    const timeText = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleTimeString() : "—");

    // The three chips of a card: last value, last change, error.
    function chipsHtml(card) {
        if (!state.savedActions.has(card.id)) return `<span class="tag neutral">Not applied yet</span>`;
        if (!state.running) return `<span class="tag warn">The server is not reading inputs</span>`;
        const row = state.status[card.id];
        if (!row) return `<span class="tag neutral">Waiting for the first read…</span>`;
        if (!row.enabled) return `<span class="tag neutral">Switched off</span>`;
        const chips = [
            `<span class="tag neutral" title="The value last read">Value<b>${esc(valueText(row.value))}</b></span>`,
            `<span class="tag neutral" title="When the value last changed">Changed<b>${esc(timeText(row.changed_at))}</b></span>`,
        ];
        if (row.error) chips.push(`<span class="tag bad" title="${esc(row.error)}">${esc(row.error)}</span>`);
        else chips.push(row.read_count ? `<span class="tag ok" title="Read ${esc(row.read_count)} times">No error</span>` : `<span class="tag neutral">Not read yet</span>`);
        return chips.join("");
    }

    function paintStatus() {
        state.cards.forEach((card) => {
            const box = byId(`plcSigChips_${card.id}`);
            if (!box) return;
            const html = chipsHtml(card);
            if (box.dataset.html === html) return;
            box.dataset.html = html;
            box.innerHTML = html;
        });
    }

    const statusWanted = () => !document.hidden && signedIn() && state.savedActions.size > 0
        && Boolean(byId("tabActionTrigger") && byId("tabActionTrigger").classList.contains("active"));

    async function refreshStatus() {
        if (state.statusBusy || !statusWanted()) return;
        const wanted = state.lineId;
        state.statusBusy = true;
        try {
            const data = await api(lineUrl("/plc-inputs/status"));
            if (wanted !== state.lineId) return;
            state.running = data.running !== false;
            state.status = Object.fromEntries((data.inputs || []).map((row) => [row.id, row]));
            paintStatus();
        } catch (_) {
            /* the next tick tries again */
        } finally {
            state.statusBusy = false;
        }
    }

    const ID_OF = "this.closest('[data-signal-id]').dataset.signalId";

    function render() {
        const container = byId("plcSignalCardsContainer");
        if (!container) return;
        const shown = view();
        [["plcSignalViewCards", "cards"], ["plcSignalViewList", "list"]].forEach(([id, value]) => {
            const btn = byId(id);
            if (!btn) return;
            btn.classList.toggle("on", value === shown);
            btn.setAttribute("aria-pressed", String(value === shown));
        });
        container.classList.toggle("plc-actions-grid", shown === "cards");
        container.classList.toggle("plc-actions-list", shown === "list");
        updateSaveState();

        if (state.loadError) {
            container.innerHTML = `
                <div class="empty-state">
                    <b>Could not load the inputs</b>
                    <p>${esc(state.loadError)}</p>
                    <button type="button" class="btn-action btn-outline" onclick="PlcSignals.load()">Try again</button>
                </div>`;
            return;
        }
        if (!state.cards.length) {
            container.innerHTML = `
                <div class="empty-state">
                    <b>No inputs yet</b>
                    <p>A signal reads a PLC address and acts on the line, for example a start button, a product sensor or the batch number the PLC holds.</p>
                    <button data-lock type="button" class="btn-action btn-blue restricted-l2" onclick="PlcSignals.add()">Add input</button>
                </div>`;
            if (typeof applyRoleRestrictions === "function") applyRoleRestrictions(container);
            return;
        }

        const onTitle = (card) => (card.enabled !== false ? "On: the server reads this signal" : "Off: not read");
        const switchHtml = (card) => `
            <label class="switch" title="${onTitle(card)}">
                <input type="checkbox" aria-label="Signal on" ${card.enabled !== false ? "checked" : ""} onchange="PlcSignals.setEnabled(${ID_OF}, this.checked)" />
                <span class="slider"></span>
            </label>`;
        const deleteHtml = `<button data-lock type="button" class="btn-action btn-sm btn-icon btn-red restricted-l2" onclick="PlcSignals.remove(${ID_OF})" title="Delete this signal" aria-label="Delete this signal">✕</button>`;
        const readHtml = `<button type="button" class="btn-action btn-sm btn-outline restricted-l2" onclick="PlcSignals.readNow(${ID_OF})" data-help="Reads this address once now and shows its value; nothing is done with it.">Read now</button>`;
        const settingsHtml = `<button type="button" class="btn-action btn-sm btn-outline" onclick="PlcSignals.openDialog(${ID_OF})">Settings</button>`;

        if (shown === "list") {
            const rows = state.cards.map((card) => {
                const s = summary(card);
                const id = esc(card.id);
                return `
                    <tr class="${card.enabled === false ? "is-off" : ""}" id="plcSig_${id}" data-signal-id="${id}">
                        <td data-label="On" class="col-switch">${switchHtml(card)}</td>
                        <td data-label="Signal"><b>${esc(card.name || "Input")}</b><span class="cell-sub">${esc(s.does)}</span></td>
                        <td data-label="Controller"><span class="cell-ellipsis" style="display:inline-block;max-width:180px;vertical-align:middle" title="${esc(s.channelName)}">${esc(s.channelName)}</span> <span class="badge ${channelOf(card) ? "badge-plc" : "badge-outline"}">${esc(s.protocol)}</span></td>
                        <td data-label="Address" class="cell-mono">${esc(s.address)}</td>
                        <td data-label="When">${esc(s.when)}<span class="cell-sub">every ${esc(s.every)}</span></td>
                        <td data-label="Now"><div class="sig-chips" id="plcSigChips_${id}"></div></td>
                        <td class="col-actions"><div class="row-actions">${settingsHtml}${readHtml}${deleteHtml}</div></td>
                    </tr>`;
            }).join("");
            container.innerHTML = `
                <div class="table-responsive"><table class="table-compact table-mobile-cards plc-list-table">
                    <thead><tr><th class="col-switch">On</th><th>Input</th><th>Controller</th><th>Address</th><th>When</th><th>Now</th><th class="col-actions"><span class="sr-only">Actions</span></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>`;
        } else {
            container.innerHTML = state.cards.map((card) => {
                const s = summary(card);
                const id = esc(card.id);
                return `
                    <div class="dispatch-card plc-action-card${card.enabled === false ? " is-off" : ""}" id="plcSig_${id}" data-signal-id="${id}">
                        <div>
                            <div class="plc-card-head">
                                <b class="plc-card-name" title="${esc(card.name || "Input")}">${esc(card.name || "Input")}</b>
                                <div class="row-actions">${switchHtml(card)}${deleteHtml}</div>
                            </div>
                            <div class="plc-target">
                                <span>PLC</span>
                                <b title="${esc(s.channelName)}">${esc(s.channelName)}</b>
                                <span class="badge ${channelOf(card) ? "badge-plc" : "badge-outline"}">${esc(s.protocol)}</span>
                            </div>
                            <div class="plc-summary">
                                <div><small>Does</small><b style="color:var(--primary-color)" title="${esc(s.does)}">${esc(s.does)}</b></div>
                                <div><small>When</small><b title="${esc(s.when)}">${esc(s.when)}</b></div>
                                <div><small>Address</small><b class="cell-mono" title="${esc(s.address)}">${esc(s.address)}</b></div>
                                <div><small>Read every</small><b>${esc(s.every)}</b></div>
                            </div>
                            <div class="sig-chips" id="plcSigChips_${id}"></div>
                        </div>
                        <div class="plc-card-actions">${settingsHtml}${readHtml}</div>
                    </div>`;
            }).join("");
        }
        paintStatus();
        if (typeof applyRoleRestrictions === "function") applyRoleRestrictions(container);
        // The camera cards list the line's triggers: tell them the inputs changed.
        document.dispatchEvent(new CustomEvent("pl:inputs"));
    }

    function add() {
        if (state.loadError) { toast("The inputs of this line could not be loaded. Try again first.", "warning"); return; }
        if (state.cards.length >= MAX_SIGNALS) { toast(`A line can have at most ${MAX_SIGNALS} inputs.`, "warning"); return; }
        const channels = plcChannels();
        const channel = channels.find((c) => !unreadable(c)) || channels[0];
        const card = {
            id: newId(),
            name: `Signal ${state.cards.length + 1}`,
            enabled: true,
            plc_endpoint_id: channel ? channel.id : "",
            address: "",
            data_type: "auto",
            poll_ms: DEFAULT_POLL_MS,
            edge: "rising",
            debounce_ms: 0,
            action: "start_line",
        };
        // Placed when its settings are OK'd; Cancel adds nothing.
        openDialog(card.id, card);
    }

    function confirmedNames(id) {
        return actionsNaming(id).map((c) => `'${c.name || c.id}'`).join(", ");
    }

    function remove(id) {
        const card = state.cards.find((c) => c.id === id);
        if (!card) return;
        const named = confirmedNames(id);
        if (named) {
            toast(`'${card.name}' confirms the output action ${named}. Choose another confirmation signal there first.`, "warning");
            return;
        }
        state.cards = state.cards.filter((c) => c.id !== id);
        if (state.draft && state.draft.id === id) closeDialog();
        render();
    }

    function setEnabled(id, on) {
        const card = state.cards.find((c) => c.id === id);
        if (!card) return;
        card.enabled = Boolean(on);
        render();
    }

    // ── Dialog ────────────────────────────────────────────────────────────────

    function openDialog(id, newCard = null) {
        const card = newCard || state.cards.find((c) => c.id === id);
        if (!card) return;
        state.draftIsNew = Boolean(newCard);
        state.draft = { ...card };
        refreshDialog();
        const modal = byId("plcSignalSettingsModal");
        if (modal) modal.classList.add("active");
    }

    function closeDialog() {
        const modal = byId("plcSignalSettingsModal");
        if (modal) modal.classList.remove("active");
        state.draft = null;
        state.draftIsNew = false;
    }

    function saveDialog() {
        const draft = state.draft;
        if (!draft) { closeDialog(); return; }
        if (takesPictures(draft)) {
            const number = Number(draft.trigger_no);
            if (!Number.isInteger(number) || number < 1 || number > MAX_TRIGGER_NO) {
                toast(`The trigger number must be a whole number from 1 to ${MAX_TRIGGER_NO}.`, "warning");
                return;
            }
            if (state.cards.some((c) => c.id !== draft.id && takesPictures(c) && Number(c.trigger_no) === number)) {
                toast(`Trigger ${number} is another input's number. Give this one its own.`, "warning");
                return;
            }
        }
        if (state.draftIsNew) state.cards.push({ ...draft, ...cleanCard(draft) });
        else {
            const index = state.cards.findIndex((c) => c.id === draft.id);
            if (index >= 0) state.cards[index] = { ...draft, ...cleanCard(draft) };
        }
        closeDialog();
        render();
    }

    // Change one field of the open card. redraw: the field decides which others show.
    function set(prop, value, redraw) {
        const draft = state.draft;
        if (!draft) return;
        draft[prop] = value;
        if (prop === "action") {
            const type = DATA_TYPES.find((t) => t.id === draft.data_type);
            if (type && type.only && !type.only.includes(value)) draft.data_type = "auto";
            if (value === "capture" && !(Number(draft.trigger_no) >= 1)) draft.trigger_no = nextTriggerNo(draft.id);
            if (value === "alarm" && !draft.alarm_severity) draft.alarm_severity = "warning";
        }
        if (prop === "name") {
            const title = byId("plcSignalModalTitle");
            if (title) title.textContent = value || "Input";
        }
        if (redraw) refreshDialog();
    }

    function refreshDialog() {
        const body = byId("plcSignalModalBody");
        const draft = state.draft;
        if (!body || !draft) return;
        const title = byId("plcSignalModalTitle");
        if (title) title.textContent = draft.name || "Input";

        const action = actionOf(draft.action);
        const channels = plcChannels();
        const channel = channelOf(draft);
        const named = confirmedNames(draft.id);
        const visionLine = lineHasVision();
        const option = (value, label, current, extra = "") => `<option value="${esc(value)}" ${value === current ? "selected" : ""} ${extra}>${esc(label)}</option>`;

        let channelOptions = option("", "— Choose a controller —", draft.plc_endpoint_id || "");
        channels.forEach((c) => {
            const dead = unreadable(c);
            channelOptions += option(c.id, `${c.name || "PLC"} [${protocolLabel(c)} - ${c.host}:${c.port}]${dead ? " · cannot be read" : ""}`,
                draft.plc_endpoint_id, dead && c.id !== draft.plc_endpoint_id ? "disabled" : "");
        });
        if (draft.plc_endpoint_id && !channel) channelOptions += option(draft.plc_endpoint_id, "(channel not found)", draft.plc_endpoint_id);

        const actionOptions = ACTIONS.map((a) => {
            // A line with a vision camera counts with that camera (the server refuses a sensor there).
            const refused = a.id === "product_sensor" && visionLine && draft.action !== "product_sensor";
            return option(a.id, a.label + (refused ? " · only on a line without a vision camera" : ""), action.id, refused ? "disabled" : "");
        }).join("");
        const typeOptions = DATA_TYPES.filter((t) => !t.only || t.only.includes(action.id) || t.id === draft.data_type)
            .map((t) => option(t.id, t.label, draft.data_type || "auto")).join("");
        const usesEdge = action.edge && draft.data_type !== "counter";

        const usedBy = takesPictures(draft) ? camerasOn(draft) : [];

        const protocol = (channel && PROTOCOLS[protocolKey(channel)]) || null;

        body.innerHTML = `
            <div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px">
                <div class="form-group" style="flex:1;margin:0">
                    <label class="form-label" for="plcSigName">Input name</label>
                    <input type="text" id="plcSigName" class="form-input" maxlength="128" value="${esc(draft.name)}" oninput="PlcSignals.set('name', this.value)" />
                </div>
                <div>
                    <label class="form-label" for="plcSigOn">On</label>
                    <label class="switch" style="margin:0 0 6px">
                        <input type="checkbox" id="plcSigOn" ${draft.enabled !== false ? "checked" : ""} onchange="PlcSignals.set('enabled', this.checked)" />
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <div class="form-group" style="margin:0">
                <label class="form-label" for="plcSigChannel">Controller${channel ? ` <span style="font-weight:600;color:var(--primary-color)">· ${esc(protocolLabel(channel))}</span>` : ""}</label>
                <select id="plcSigChannel" class="form-input" onchange="PlcSignals.set('plc_endpoint_id', this.value, true)">${channelOptions}</select>
                ${unreadable(channel) ? `<div class="sig-note warn">This channel is Generic TCP and cannot be read. Choose another channel.</div>` : ""}
                <div class="sig-note">${channels.length ? "" : "No controller yet: add one under Connections. "}A Generic TCP channel cannot be read: it only sends.</div>
            </div>

            <div class="form-group" style="margin:0">
                <label class="form-label" for="plcSigAction">What the signal does</label>
                <select id="plcSigAction" class="form-input" ${named ? "disabled" : ""} onchange="PlcSignals.set('action', this.value, true)" data-help="What the line does with this input, for example start the line or take a picture.">${actionOptions}</select>
                <div class="sig-note">${esc(action.note)}</div>
                ${action.id === "confirm_card" ? `<div class="sig-note">${named
                    ? `<b>Confirms:</b> ${esc(named)}. To change what this signal does or delete it, first choose another confirmation signal in ${actionsNaming(draft.id).length === 1 ? "that action" : "those actions"}.`
                    : "No output action names this signal yet. Pick it as the <b>Confirmation signal</b> in an output action's settings."}</div>` : ""}
                ${action.id === "product_sensor" && visionLine ? `<div class="sig-note warn">This line has a vision camera, which counts the products. A product sensor is only for a line without one; use Take a picture to trigger a camera.</div>` : ""}
            </div>

            <div class="sig-grid">
                <div class="form-group">
                    <label class="form-label" for="plcSigAddress">Address / tag</label>
                    <input type="text" id="plcSigAddress" class="form-input" maxlength="128" value="${esc(draft.address)}" placeholder="${esc(protocol ? "e.g. " + protocol[1] : "Address or tag")}" oninput="PlcSignals.set('address', this.value)" />
                    ${protocol ? `<div class="sig-note">${esc(protocol[2])}</div>` : ""}
                </div>
                <div class="form-group">
                    <label class="form-label" for="plcSigType">Read as</label>
                    <select id="plcSigType" class="form-input" onchange="PlcSignals.set('data_type', this.value, true)" data-help="How the value at the address is read: a bit, a number, a counter or text.">${typeOptions}</select>
                    ${draft.data_type === "string" ? `<div class="sig-note">Text can be read from OPC UA and EtherNet/IP channels.</div>` : ""}
                    ${draft.data_type === "counter" ? `<div class="sig-note">The increase of the counter since the last read is the number of products. It may wrap at 65535; a drop to a low number is taken as the PLC resetting it.</div>` : ""}
                </div>
            </div>

            ${usesEdge ? `
            <div class="form-group" style="margin:0">
                <label class="form-label" for="plcSigEdge">Act when the value goes</label>
                <select id="plcSigEdge" class="form-input" onchange="PlcSignals.set('edge', this.value)" data-help="Which change of the value makes the input act.">${EDGES.map((e) => option(e.id, e.label, draft.edge || "rising")).join("")}</select>
            </div>` : ""}

            <div class="sig-grid">
                <div class="form-group">
                    <label class="form-label" for="plcSigPoll">Read every (ms)</label>
                    <input type="number" id="plcSigPoll" class="form-input" min="50" max="10000" step="10" value="${esc(draft.poll_ms ?? DEFAULT_POLL_MS)}" oninput="PlcSignals.set('poll_ms', this.value)" />
                </div>
                <div class="form-group">
                    <label class="form-label" for="plcSigDebounce">Debounce (ms)</label>
                    <input type="number" id="plcSigDebounce" class="form-input" min="0" max="5000" step="10" value="${esc(draft.debounce_ms ?? 0)}" oninput="PlcSignals.set('debounce_ms', this.value)" />
                </div>
            </div>
            ${draft.data_type !== "counter" ? `
            <div class="form-group" style="margin:0">
                <label class="form-label" for="plcSigValueName">Value name (optional)</label>
                <input type="text" id="plcSigValueName" class="form-input" maxlength="32" value="${esc(draft.value_name || "")}" placeholder="for example speed" oninput="PlcSignals.set('value_name', this.value.trim())">
                <div class="sig-note">Use the value this input reads in formulas and output actions as <b>input.&lt;value name&gt;</b>: letters, digits and _, starting with a letter.</div>
            </div>` : ""}

            <div class="sig-note" style="margin-top:-8px">The signals of one channel are read together, at the shortest time among them (50 to 10000 ms). A new value counts once it has lasted the debounce time (0 to 5000 ms). A sensor that switches faster than the read time is missed: let the PLC count the products and read its counter with <b>Product sensor</b> and the type <b>PLC counter</b>.</div>

            ${action.id === "capture" ? `
            <div class="form-group" style="margin:0">
                <label class="form-label" for="plcSigTrigger">Trigger number</label>
                <input type="number" id="plcSigTrigger" class="form-input" min="1" max="99" step="1" value="${esc(draft.trigger_no || "")}" oninput="PlcSignals.set('trigger_no', parseInt(this.value, 10) || '')">
                <div class="sig-note${usedBy.length ? "" : " warn"}">${usedBy.length
                    ? `Takes a picture with: ${esc(usedBy.join(", "))}.`
                    : "No camera uses this trigger yet."} A camera chooses its triggers on its own card: under <b>Counting</b> (Count mode <b>One picture per trigger</b>) or <b>Read codes</b> (<b>When an input asks</b>).</div>
            </div>` : ""}

            ${action.id === "alarm" ? `
            <div class="sig-grid">
                <div class="form-group">
                    <label class="form-label" for="plcSigAlarmText">Alarm text</label>
                    <input type="text" id="plcSigAlarmText" class="form-input" maxlength="200" value="${esc(draft.alarm_text)}" placeholder="${esc(draft.name || "The signal's name")}" oninput="PlcSignals.set('alarm_text', this.value)" />
                    <div class="sig-note">Shown in the Alarms panel. Empty: the signal's name.</div>
                </div>
                <div class="form-group">
                    <label class="form-label" for="plcSigSeverity">Severity</label>
                    <select id="plcSigSeverity" class="form-input" onchange="PlcSignals.set('alarm_severity', this.value)">${SEVERITIES.map((s) => option(s.id, s.label, draft.alarm_severity || "warning")).join("")}</select>
                </div>
            </div>` : ""}

            <div class="sig-read">
                <button type="button" class="btn-action btn-sm btn-outline restricted-l2" onclick="PlcSignals.readNow(PlcSignals.editingId())" data-help="Reads this address once now and shows its value; nothing is done with it.">Read now</button>
                <span class="sig-read-result" id="plcSigReadResult" role="status" aria-live="polite">Reads the address once and shows its value. Nothing the signal would do is done.</span>
            </div>`;
        if (typeof applyRoleRestrictions === "function") applyRoleRestrictions(body);
    }

    // The dialog's and the cards' "Read now": the card as it is on screen, saved or not.
    async function readNow(id) {
        const inDialog = Boolean(state.draft && state.draft.id === id);
        const card = inDialog ? state.draft : state.cards.find((c) => c.id === id);
        if (!card) return;
        const show = (text, ok) => {
            const out = byId("plcSigReadResult");
            if (state.draft && state.draft.id === id && out) {
                out.className = `sig-read-result ${ok === null ? "" : ok ? "ok" : "bad"}`;
                out.textContent = text;
            } else if (ok !== null) {
                toast(`${card.name || "Input"}: ${text}`, ok ? "success" : "warning");
            }
        };
        show("Reading…", null);
        try {
            const res = await api(lineUrl(`/plc-inputs/${encodeURIComponent(id)}/read`), { method: "POST", body: { input: cleanCard(card) } });
            if (res.ok) show(`Value: ${valueText(res.value)}`, true);
            else show(res.message || "The address could not be read.", false);
        } catch (e) {
            show(e.message, false);
        }
    }

    // The line's "Confirm a PLC action" signals, for the PLC action dialog's Confirmation signal.
    function confirmSignals() {
        return state.cards.filter((card) => card.action === "confirm_card").map((card) => ({
            id: card.id,
            name: (card.name || card.id) + (state.savedActions.get(card.id) === "confirm_card" ? "" : " (not applied yet)"),
        }));
    }

    // The line's inputs that name their value, for the output action dialog's "Input" value.
    function valueInputs() {
        return state.cards.filter((card) => String(card.value_name || "").trim())
            .map((card) => ({ value_name: String(card.value_name).trim(), name: card.name || card.id }));
    }

    window.PlcSignals = {
        load,
        valueInputs,
        // Every input of the line, for the trigger "Input changed".
        inputChoices: () => state.cards.map((card) => ({ id: card.id, name: card.name || card.id })),
        render,
        add,
        apply,
        openDialog,
        readNow,
        confirmSignals,
        triggers,
        hasPending,
        // A product sensor input makes the line's products, as a vision camera would (Sync needs one or the other).
        hasProductSensor: () => state.cards.some((c) => c.action === "product_sensor"),
        setView,
        setEnabled,
        remove,
        set,
        saveDialog,
        closeDialog,
        editingId: () => (state.draft ? state.draft.id : null),
    };
})();
