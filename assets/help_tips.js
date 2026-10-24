/*
 * Help icons. A control with data-help="One plain sentence." gets a small
 * circled "i" right after it; hovering the icon, giving it keyboard focus or
 * tapping it shows the sentence in one shared bubble kept inside the window.
 * Menu items and segmented buttons take the icon inside, at their end, where
 * an element beside them would break their list, group or grid.
 *
 * Controls drawn later (cards, dialogs, tables) get their icon from a
 * MutationObserver. A control never gets a second icon: addIcons() finds the
 * one it has, also when a drop-down's on-screen box (initCustomSelects in
 * dashboard.html) came between them, and moves it back when the control moved.
 *
 * Runs in node for tests/test_help_tips.py (module.exports), like card_values.js.
 */
(function (root) {
    "use strict";

    const ICON = "help-tip";
    const KEY = "__helpTip";           // the icon a control was given, kept on the control
    const MARGIN = 8;                  // the bubble keeps this far from the window's edges
    const GAP = 6;                     // and this far from its icon
    const INSIDE_CLASSES = ["proto-btn", "filter-pill", "pl-fold-head"];
    const INSIDE_PARENTS = ["seg"];
    const FOCUSABLE = ["BUTTON", "SELECT", "A"];

    // Menu items, segmented buttons and grid tiles take the icon inside.
    function goesInside(control) {
        if (control.tagName === "LI" || control.getAttribute("role") === "menuitem"
            || control.getAttribute("data-help-at") === "inside") return true;
        if (INSIDE_CLASSES.some((name) => control.classList.contains(name))) return true;
        const parent = control.parentElement;
        return Boolean(parent && INSIDE_PARENTS.some((name) => parent.classList.contains(name)));
    }

    // What the icon follows: a drop-down's on-screen box when it has one, else the control.
    function anchorOf(control) {
        const next = control.nextElementSibling;
        return control.tagName === "SELECT" && next && next.classList.contains("custom-select-wrap") ? next : control;
    }

    // The icon in its place beside (or inside) the control, or null.
    function iconOf(control) {
        if (goesInside(control)) {
            for (const child of control.children) if (child.classList.contains(ICON)) return child;
            return null;
        }
        const next = anchorOf(control).nextElementSibling;
        return next && next.classList.contains(ICON) ? next : null;
    }

    function place(control, icon) {
        if (goesInside(control)) control.appendChild(icon);
        else anchorOf(control).insertAdjacentElement("afterend", icon);
    }

    function makeIcon(doc, control) {
        const icon = doc.createElement("span");
        icon.className = ICON;
        icon.setAttribute("role", "img");
        // Inside a button the button's own keyboard focus shows the text; elsewhere the icon takes focus.
        if (!(goesInside(control) && FOCUSABLE.includes(control.tagName))) icon.setAttribute("tabindex", "0");
        return icon;
    }

    // Gives every control with data-help in scope (scope included) its one icon. Returns the icons added.
    function addIcons(scope) {
        if (!scope || !scope.querySelectorAll) return 0;
        const doc = scope.ownerDocument || scope;
        const controls = Array.from(scope.querySelectorAll("[data-help]"));
        if (scope.hasAttribute && scope.hasAttribute("data-help")) controls.unshift(scope);
        let added = 0;
        for (const control of controls) {
            const text = (control.getAttribute("data-help") || "").trim();
            if (!text) continue;
            let icon = iconOf(control);
            if (!icon && control[KEY] && control[KEY].parentElement) {
                icon = control[KEY];           // the control moved: its icon goes with it
                place(control, icon);
            }
            if (!icon) {
                icon = makeIcon(doc, control);
                place(control, icon);
                added += 1;
            }
            control[KEY] = icon;
            if (icon.getAttribute("aria-label") !== text) icon.setAttribute("aria-label", text);
            const anchor = anchorOf(control);
            if (anchor !== control && !anchor.classList.contains("has-help")) anchor.classList.add("has-help");
        }
        return added;
    }

    // Where the bubble goes: centred below the icon, above it when there is no room below,
    // and pushed back inside the window near an edge. anchor: the icon's rectangle.
    function placeBubble(anchor, size, view) {
        let top = anchor.bottom + GAP;
        const above = top + size.height > view.height - MARGIN && anchor.top - GAP - size.height >= MARGIN;
        if (above) top = anchor.top - GAP - size.height;
        const centred = anchor.left + anchor.width / 2 - size.width / 2;
        const left = Math.max(MARGIN, Math.min(centred, view.width - MARGIN - size.width));
        return { left: Math.round(left), top: Math.round(top), above };
    }

    // ── In the page ──────────────────────────────────────────────────────────

    let bubble = null;
    let shown = null;

    function show(icon) {
        const doc = icon.ownerDocument;
        if (!bubble) {
            bubble = doc.createElement("div");
            bubble.className = "help-tip-bubble";
            bubble.id = "helpTipBubble";
            bubble.setAttribute("role", "tooltip");
            doc.body.appendChild(bubble);
        }
        bubble.textContent = icon.getAttribute("aria-label") || "";
        bubble.style.left = "0px";
        bubble.style.top = "0px";
        bubble.hidden = false;
        const spot = placeBubble(icon.getBoundingClientRect(), bubble.getBoundingClientRect(),
            { width: doc.documentElement.clientWidth, height: root.innerHeight });
        bubble.style.left = `${spot.left}px`;
        bubble.style.top = `${spot.top}px`;
        bubble.classList.toggle("above", spot.above);
        shown = icon;
    }

    function hide() {
        if (bubble) bubble.hidden = true;
        shown = null;
    }

    const tipOf = (target) => (target && target.closest ? target.closest(`.${ICON}`) : null);

    // A button that holds its icon, focused from the keyboard (not by a click).
    function ownIcon(target) {
        if (!target || !target.hasAttribute || !target.hasAttribute("data-help") || !goesInside(target)) return null;
        try { if (!target.matches(":focus-visible")) return null; } catch (_) { return null; }
        return iconOf(target);
    }

    function init(doc) {
        addIcons(doc.body);
        new MutationObserver((records) => {
            const scopes = new Set();
            records.forEach((r) => {
                const target = r.target.nodeType === 1 ? r.target : r.target.parentElement;
                if (target && target !== bubble) scopes.add(target);
            });
            scopes.forEach((scope) => { if (scope.isConnected) addIcons(scope); });
        }).observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-help"] });

        doc.addEventListener("mouseover", (e) => {
            const tip = tipOf(e.target);
            if (tip && tip !== shown) show(tip);
        });
        doc.addEventListener("mouseout", (e) => {
            const tip = tipOf(e.target);
            if (tip && tip === shown && !tip.contains(e.relatedTarget)) hide();
        });
        doc.addEventListener("focusin", (e) => {
            const tip = tipOf(e.target) || ownIcon(e.target);
            if (tip) show(tip);
            else if (shown) hide();
        });
        doc.addEventListener("focusout", () => { if (shown) hide(); });
        // A click or tap on an icon shows its text and nothing else: the control around it is not pressed.
        doc.addEventListener("click", (e) => {
            const tip = tipOf(e.target);
            if (tip) {
                e.preventDefault();
                e.stopPropagation();
                show(tip);
            } else if (shown) {
                hide();
            }
        }, true);
        doc.addEventListener("keydown", (e) => { if (e.key === "Escape" && shown) hide(); });
        root.addEventListener("scroll", () => { if (shown) hide(); }, true);
        root.addEventListener("resize", () => { if (shown) hide(); });
    }

    const api = { ICON, addIcons, iconOf, placeBubble };
    root.HelpTips = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;

    if (typeof document !== "undefined" && typeof MutationObserver !== "undefined") {
        if (document.body) init(document);
        else document.addEventListener("DOMContentLoaded", () => init(document));
    }
})(typeof window !== "undefined" ? window : globalThis);
