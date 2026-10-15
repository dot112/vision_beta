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

    const api = { VALUE_NAMES, FORMULA_EXAMPLES, valueChoices, fieldChoices, valueSourceText, writesText };
    root.CardValues = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
