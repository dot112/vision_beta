/* Card dialogs: the value names a card can use, and how a card says what it writes.
 *
 * The names are the server's (app/services/event_values.py): result, class,
 * code, product.<field>, input.<value name>... They are used by the value an
 * output action writes, by formulas and by only-when rules.
 *
 * window.CardValues, and module.exports for the tests (run with node).
 */
(function (root) {
    "use strict";

    const VALUE_NAMES = [
        { name: "result", label: "Result (good / reject)" },
        { name: "result_code", label: "Result code (1 good, 2 reject)" },
        { name: "reject_reason", label: "Reject reason" },
        { name: "class", label: "Class the camera saw" },
        { name: "confidence", label: "Confidence (0 to 1)" },
        { name: "code", label: "Code read" },
        { name: "code_status", label: "Code status (known / unknown / no_read)" },
        { name: "product_id", label: "Product ID in the code" },
        { name: "sequence", label: "Product sequence (from the PLC)" },
        { name: "good_count", label: "Good count" },
        { name: "reject_count", label: "Reject count" },
        { name: "total_count", label: "Total count" },
        { name: "batch", label: "Batch number" },
        { name: "line", label: "Line name" },
        { name: "camera", label: "Camera name" },
    ];

    const FORMULA_EXAMPLES = [
        ['if(result == "good", product.lane, 9)', "the product's lane, or 9 for a reject"],
        ["round(input.speed / 4, 1)", "an input's value, worked out"],
        ['text(product.lane) + "-" + product.recipe', "text made of two values (OPC UA String tags only)"],
        ["good_count % 100", "the good count, from 0 to 99"],
    ];

    const reads = (camera) => camera.role === "qr" || !!camera.read_codes;

    // Every value name a card can use: the plain names, the product's own, its fields, the line's input values.
    function valueChoices(context) {
        const fields = (context && context.fields) || [];
        const inputs = (context && context.inputs) || [];
        return [
            ...VALUE_NAMES,
            { name: "product.name", label: "Product name" },
            { name: "product.code", label: "Product code" },
            { name: "product.list", label: "Product list" },
            ...fields.map((f) => ({ name: `product.${f.key}`, label: `${f.label || f.key}${f.list ? ` (${f.list})` : ""}` })),
            ...inputs.filter((i) => i.value_name).map((i) => ({ name: `input.${i.value_name}`, label: `Input: ${i.name || i.value_name}` })),
        ];
    }

    // The fields of the product lists the line's code readers check against, each key once: [{key, label, type, list}].
    function fieldChoices(cameras, productLists) {
        const wanted = new Set((cameras || []).filter(reads).map((c) => c.product_list_id).filter(Boolean));
        const seen = new Set();
        const found = [];
        for (const list of productLists || []) {
            if (!wanted.has(list.id)) continue;
            for (const field of list.fields || []) {
                if (seen.has(field.key)) continue;
                seen.add(field.key);
                found.push({ key: field.key, label: field.label || field.key, type: field.type, list: list.name });
            }
        }
        return found;
    }

    // What a card of one of the new value sources writes, in words; null for the other sources.
    function valueSourceText(card) {
        switch (card && card.value_source) {
            case "product_field": return `product.${card.value_field || "?"}`;
            case "input": return `input.${card.value_input || "?"}`;
            case "formula": return "a formula";
            case "code": return "the code";
            case "product_name": return "the product name";
            default: return null;
        }
    }

    // Whether the card writes text, which only an OPC UA controller takes.
    function writesText(card, fields) {
        const source = card && card.value_source;
        if (source === "code" || source === "product_name") return true;
        if (source === "product_field") {
            const field = (fields || []).find((f) => f.key === card.value_field);
            return !!field && field.type === "text";
        }
        return false;
    }

    // ── Only when: up to 5 rules on the event's values (app/services/rules.py) ──

    const MAX_RULES = 5;
    const RULE_OPS = [["=", "="], ["!=", "≠"], ["<", "<"], ["<=", "≤"], [">", ">"], [">=", "≥"],
        ["contains", "contains"], ["empty", "is empty"], ["not_empty", "is not empty"]];
    const opLabel = (op) => (RULE_OPS.find(([id]) => id === op) || [op, op])[1];
    const noRight = (op) => op === "empty" || op === "not_empty";
    const esc = (value) => String(value === null || value === undefined ? "" : value)
        .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

    function ruleText(rule) {
        if (noRight(rule.op)) return `${rule.left} ${opLabel(rule.op)}`;
        const right = rule.right_kind === "value" ? rule.right : (rule.right === "" ? '""' : rule.right);
        return `${rule.left} ${opLabel(rule.op)} ${right}`;
    }

    // "only when class ≠ product.expected_class or product.lane = 3"; "" without rules.
    function onlyWhenText(onlyWhen) {
        const rules = (onlyWhen && onlyWhen.rules) || [];
        if (!rules.length) return "";
        return "only when " + rules.map(ruleText).join(onlyWhen.match === "any" ? " or " : " and ");
    }

    // The editor. handler: the name of a page function called as handler(element) on a change and
    // handler(element, 'add' | 'remove') on the buttons; null when the page listens itself (delegation).
    function onlyWhenHtml(onlyWhen, choices, handler, options) {
        const rules = (onlyWhen && onlyWhen.rules) || [];
        const match = onlyWhen && onlyWhen.match === "any" ? "any" : "all";
        const on = (event) => (handler ? ` ${event}="${handler}(this)"` : "");
        const click = (action) => (handler ? ` onclick="${handler}(this, '${action}')"` : "");
        const opt = (value, label, current) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
        const names = (current) => {
            const list = (choices || []).map((c) => opt(c.name, `${c.label} (${c.name})`, current));
            if (current && !(choices || []).some((c) => c.name === current)) list.push(opt(current, `${current} (not on this line)`, current));
            return list.join("");
        };
        const rows = rules.map((rule, index) => {
            const kind = rule.right_kind === "value" ? "value" : "constant";
            const right = rule.right === undefined || rule.right === null ? "" : rule.right;
            const tail = noRight(rule.op) ? "<span></span><span></span>" : `
                <select class="form-input ow-kind" aria-label="Compare with"${on("onchange")}>${opt("constant", "a constant", kind)}${opt("value", "a value", kind)}</select>
                ${kind === "value"
                    ? `<select class="form-input ow-right" aria-label="Value to compare with"${on("onchange")}>${right ? "" : `<option value="" selected>Choose a value</option>`}${names(right)}</select>`
                    : `<input type="text" class="form-input ow-right" maxlength="256" aria-label="Constant" value="${esc(right)}"${on("oninput")}>`}`;
            return `<div class="ow-row" data-index="${index}">
                <select class="form-input ow-left" aria-label="Value"${on("onchange")}>${names(rule.left)}</select>
                <select class="form-input ow-op" aria-label="Operator"${on("onchange")}>${RULE_OPS.map(([id, label]) => opt(id, label, rule.op)).join("")}</select>
                ${tail}
                <button type="button" class="btn-action btn-outline ow-del" data-index="${index}"${click("remove")}>Remove</button>
            </div>`;
        }).join("");
        const empty = (options && options.emptyText) || "No rules: it acts every time.";
        return `<div class="ow-block" data-ow>
            <div class="ow-head"><span class="form-label" style="margin:0">Only when</span>
                <select class="form-input ow-match" aria-label="Which rules must pass"${on("onchange")} data-help="Whether all of the rules below must pass, or any one of them.">${opt("all", "all of these rules pass", match)}${opt("any", "any of these rules passes", match)}</select></div>
            <div class="ow-rows">${rows || `<div class="ow-none">${esc(empty)}</div>`}</div>
            <button type="button" class="btn-action btn-outline ow-add"${rules.length >= MAX_RULES ? " disabled" : ""}${click("add")} data-help="Adds a rule to the list above." data-help-at="inside">Add rule</button>
        </div>`;
    }

    // The rules as shown in an editor ([data-ow]).
    function readOnlyWhen(block) {
        if (!block) return null;
        const rules = Array.from(block.querySelectorAll(".ow-row")).map((row) => {
            const kind = row.querySelector(".ow-kind");
            const right = row.querySelector(".ow-right");
            return { left: row.querySelector(".ow-left").value, op: row.querySelector(".ow-op").value,
                right: right ? right.value : "", right_kind: kind ? kind.value : "constant" };
        });
        return { match: block.querySelector(".ow-match").value, rules };
    }

    // The rules after a change in the editor (action: 'add' | 'remove' | undefined); null without rules.
    function applyEdit(block, element, action, firstName) {
        const onlyWhen = readOnlyWhen(block) || { match: "all", rules: [] };
        if (action === "add" && onlyWhen.rules.length < MAX_RULES) {
            onlyWhen.rules.push({ left: firstName || "result", op: "=", right: "", right_kind: "constant" });
        } else if (action === "remove") {
            onlyWhen.rules.splice(Number(element.dataset.index), 1);
        } else if (element && element.classList && element.classList.contains("ow-kind")) {
            onlyWhen.rules[Number(element.closest(".ow-row").dataset.index)].right = "";
        }
        return onlyWhen.rules.length ? onlyWhen : null;
    }

    // Whether the editor must be drawn again after this change (its rows or controls change).
    const needsRedraw = (element, action) => !!action
        || !!(element && element.classList && (element.classList.contains("ow-kind") || element.classList.contains("ow-op")));

    const api = { VALUE_NAMES, FORMULA_EXAMPLES, MAX_RULES, RULE_OPS, valueChoices, fieldChoices, valueSourceText, writesText,
        onlyWhenText, onlyWhenHtml, readOnlyWhen, applyEdit, needsRedraw };
    root.CardValues = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
