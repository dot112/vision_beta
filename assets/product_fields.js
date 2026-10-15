/* Products page: a product list's own fields and a product's values for them.
 *
 * A list has up to 20 fields ({key, label, type}: number, text or yes_no).
 * The key names a field in cards, formulas and messages (product.lane), so it
 * is fixed once the field is saved; the label can change. The server checks
 * everything again (app/services/product_fields.py); these helpers only build
 * the editors and read them back.
 *
 * window.ProductFields, and module.exports for the tests (run with node).
 */
(function (root) {
    "use strict";

    const TYPES = [["number", "Number"], ["text", "Text"], ["yes_no", "Yes or no"]];
    const RESERVED = ["name", "code", "list", "description"];
    const MAX_FIELDS = 20;
    const NUMBER = /^[+-]?(\d+(\.\d*)?|\.\d+)$/;

    const esc = (value) => String(value === null || value === undefined ? "" : value)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    // A key made from a label, as the server makes one from a CSV header: "Expected class" -> "expected_class".
    function keyFromLabel(label, taken) {
        let key = String(label || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "field";
        if (/^[0-9]/.test(key)) key = "f_" + key;
        key = key.slice(0, 32).replace(/_+$/, "");
        const used = new Set(taken || []);
        let candidate = key;
        for (let n = 2; used.has(candidate) || RESERVED.includes(candidate); n++) {
            const suffix = `_${n}`;
            candidate = key.slice(0, 32 - suffix.length) + suffix;
        }
        return candidate;
    }

    // A value as the table shows it: yes / no, a number as written, "" when it is missing.
    function cellText(field, value) {
        if (value === undefined || value === null || value === "") return "";
        if (typeof value === "boolean") return value ? "yes" : "no";
        return String(value);
    }

    // What was typed for each field ({key: text}) as the values to send: {data, error}.
    function valuesFromInputs(fields, typed) {
        const data = {};
        for (const field of fields || []) {
            const text = String((typed || {})[field.key] ?? "").trim();
            if (!text) continue;
            if (field.type === "number") {
                if (!NUMBER.test(text)) return { data: null, error: `${field.label || field.key}: enter a number` };
                data[field.key] = Number(text);
            } else if (field.type === "yes_no") {
                data[field.key] = text === "yes";
            } else {
                data[field.key] = text;
            }
        }
        return { data: Object.keys(data).length ? data : null, error: null };
    }

    // ── The fields editor ─────────────────────────────────────────────────────

    function fieldRowHtml(field, saved) {
        const type = field.type || "text";
        return `<div class="pf-row" data-saved="${saved ? "1" : "0"}" data-type="${esc(saved ? type : "")}">
            <input class="form-input pf-label" maxlength="64" placeholder="Label" aria-label="Label" value="${esc(field.label || "")}">
            <input class="form-input pf-key" maxlength="32" placeholder="key" aria-label="Key" value="${esc(field.key || "")}"
                ${saved ? "readonly title=\"A saved field keeps its key\"" : "data-auto=\"1\""}>
            <select class="form-input pf-type" aria-label="Type">${TYPES.map(([id, label]) =>
                `<option value="${id}" ${id === type ? "selected" : ""}>${label}</option>`).join("")}</select>
            <div class="pf-buttons">
                <button type="button" class="btn-action btn-outline pf-up" title="Move up" aria-label="Move up">&#8593;</button>
                <button type="button" class="btn-action btn-outline pf-down" title="Move down" aria-label="Move down">&#8595;</button>
                <button type="button" class="btn-action btn-red pf-del">Delete</button>
            </div>
        </div>`;
    }

    function fieldsEditorHtml(fields) {
        return `<div class="pf-note">Use a field in output actions, formulas and messages by its key, as <span class="pl-code">product.&lt;key&gt;</span>. A saved field keeps its key; its label can change.</div>
            <div class="pf-head"><span>Label</span><span>Key</span><span>Type</span><span></span></div>
            <div class="pf-rows">${(fields || []).map((f) => fieldRowHtml(f, true)).join("")}</div>
            <button type="button" class="btn-action btn-outline pf-add">Add field</button>
            <div class="pf-count"></div>`;
    }

    function rowsOf(container) {
        return Array.from(container.querySelectorAll(".pf-row"));
    }

    function updateCount(container) {
        const n = rowsOf(container).length;
        container.querySelector(".pf-count").textContent = `${n} of ${MAX_FIELDS} fields`;
        container.querySelector(".pf-add").disabled = n >= MAX_FIELDS;
    }

    // Fill a container with the editor and wire its buttons. Returns {read, changesValues}.
    function mountFieldsEditor(container, fields) {
        container.innerHTML = fieldsEditorHtml(fields);
        const list = container.querySelector(".pf-rows");
        container.querySelector(".pf-add").addEventListener("click", () => {
            if (rowsOf(container).length >= MAX_FIELDS) return;
            list.insertAdjacentHTML("beforeend", fieldRowHtml({ type: "text" }, false));
            updateCount(container);
            const rows = rowsOf(container);
            rows[rows.length - 1].querySelector(".pf-label").focus();
        });
        container.addEventListener("click", (e) => {
            const row = e.target.closest(".pf-row");
            if (!row) return;
            if (e.target.closest(".pf-del")) row.remove();
            else if (e.target.closest(".pf-up") && row.previousElementSibling) list.insertBefore(row, row.previousElementSibling);
            else if (e.target.closest(".pf-down") && row.nextElementSibling) list.insertBefore(row.nextElementSibling, row);
            updateCount(container);
        });
        container.addEventListener("input", (e) => {
            const row = e.target.closest(".pf-row");
            if (!row) return;
            const keyInput = row.querySelector(".pf-key");
            if (e.target === keyInput) { delete keyInput.dataset.auto; return; }
            if (e.target.classList.contains("pf-label") && keyInput.dataset.auto) {
                const others = rowsOf(container).filter((r) => r !== row).map((r) => r.querySelector(".pf-key").value.trim());
                keyInput.value = e.target.value.trim() ? keyFromLabel(e.target.value, others) : "";
            }
        });
        updateCount(container);
        const savedKeys = (fields || []).map((f) => f.key);
        return {
            // The fields as typed, in their order (a row with neither label nor key is left out).
            read() {
                return rowsOf(container).map((row) => ({
                    key: row.querySelector(".pf-key").value.trim(),
                    label: row.querySelector(".pf-label").value.trim(),
                    type: row.querySelector(".pf-type").value,
                })).filter((f) => f.key || f.label).map((f) => ({ ...f, label: f.label || f.key }));
            },
            // True when saving removes a field or changes a saved field's type: values may be lost.
            changesValues() {
                const rows = rowsOf(container);
                const kept = rows.filter((r) => r.dataset.saved === "1");
                return kept.length < savedKeys.length
                    || kept.some((r) => r.querySelector(".pf-type").value !== r.dataset.type);
            },
        };
    }

    // ── A product's values ────────────────────────────────────────────────────

    function dataInputsHtml(fields, data) {
        const values = data || {};
        return (fields || []).map((f) => {
            const id = `pfv-${esc(f.key)}`;
            const value = values[f.key];
            let input;
            if (f.type === "yes_no") {
                const chosen = value === true ? "yes" : value === false ? "no" : "";
                input = `<select class="form-input pf-val" id="${id}" data-key="${esc(f.key)}">${[["", "(empty)"], ["yes", "Yes"], ["no", "No"]]
                    .map(([v, l]) => `<option value="${v}" ${v === chosen ? "selected" : ""}>${l}</option>`).join("")}</select>`;
            } else {
                const attrs = f.type === "number" ? `inputmode="decimal"` : `maxlength="256"`;
                input = `<input type="text" class="form-input pf-val" id="${id}" data-key="${esc(f.key)}" ${attrs} value="${esc(cellText(f, value))}">`;
            }
            return `<div class="form-group"><label class="form-label" for="${id}">${esc(f.label || f.key)} <span class="pf-key-hint">product.${esc(f.key)}</span></label>${input}</div>`;
        }).join("");
    }

    function readDataInputs(container, fields) {
        const typed = {};
        container.querySelectorAll(".pf-val").forEach((input) => { typed[input.dataset.key] = input.value; });
        return valuesFromInputs(fields, typed);
    }

    const api = { MAX_FIELDS, TYPES, keyFromLabel, cellText, valuesFromInputs, fieldsEditorHtml, mountFieldsEditor, dataInputsHtml, readDataInputs };
    root.ProductFields = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
