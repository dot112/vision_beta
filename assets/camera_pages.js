/*
 * Line setup: a camera's settings page. The small card on Line setup shows the camera; its Settings
 * button opens the camera's page, with a menu of pages (Camera, Job, AI model and classes, Counting,
 * Codes, Text fields, Image and video) of which only those that fit the camera's job are listed.
 *
 * This file decides what can be decided without the page: which pages a job shows and their titles,
 * which page opens, the card's summary line, the menu's markup, and whether a page drawn again opens
 * the same camera. production_lines.js draws the card and switches between the cards and a page.
 * A job's kind is { vision, codes, text }: a vision job, a job that reads codes, the text reader.
 * Runs in node for tests/test_camera_pages.py (module.exports), like text_reader.js.
 */
(function (root) {
    "use strict";

    const PAGES = [
        { id: "camera", title: "Camera" },
        { id: "job", title: "Job" },
        { id: "model", title: "AI model and classes" },
        { id: "counting", title: "Counting" },
        { id: "codes", title: "Codes" },
        { id: "text", title: "Text fields" },
        { id: "image", title: "Image and video" },
    ];
    // What a code reader does with its codes (Read codes), for the summary line.
    const CODE_TRIGGERS = {
        continuous: "Shows codes",
        line1: "Reads on wire line 1",
        line2: "Reads on wire line 2",
        signal: "Reads when an input asks",
    };
    // The page each problem that stops Save opens.
    const PROBLEM_PAGES = { no_model: "model", no_list: "codes", no_yolo: "text", no_format: "text" };

    function fits(id, kind) {
        if (id === "model") return Boolean(kind.vision);
        if (id === "counting") return Boolean(kind.vision || kind.text);
        if (id === "codes") return Boolean(kind.codes);
        if (id === "text") return Boolean(kind.text);
        return PAGES.some((p) => p.id === id);
    }

    const pagesFor = (kind) => PAGES.filter((p) => fits(p.id, kind)).map((p) => p.id);

    // A text reader's Counting page is its Reading (continuously or one picture per trigger).
    function pageTitle(id, kind) {
        if (id === "counting" && kind.text) return "Reading";
        const page = PAGES.find((p) => p.id === id);
        return page ? page.title : "";
    }

    // The page asked for when it fits, else the one this camera showed last, else Camera.
    function pageToShow({ asked, remembered }, kind) {
        if (asked && fits(asked, kind)) return asked;
        if (remembered && fits(remembered, kind)) return remembered;
        return "camera";
    }

    const pageAfterJobChange = (current, kind) => (fits(current, kind) ? current : "job");

    // A camera just added opens where it needs filling in first.
    function startPage(kind) {
        if (kind.vision) return "model";
        if (kind.text) return "text";
        if (kind.codes) return "codes";
        return "camera";
    }

    const pageForProblem = (problem) => PROBLEM_PAGES[problem] || "camera";

    // The card's summary line: "Camera 2 · IP · One picture per trigger".
    function summary({ index, type, kind, countMode, joined, codeTrigger }) {
        const triggered = countMode === "trigger";
        const what = kind.text ? (triggered ? "One picture per trigger" : "Shows text")
            : kind.vision ? (triggered ? "One picture per trigger" : "Count lines")
            : CODE_TRIGGERS[codeTrigger] || CODE_TRIGGERS.continuous;
        return [`Camera ${index + 1}`, type, what, joined ? "joined" : ""].filter(Boolean).join(" · ");
    }

    // Every page has its link; those that do not fit the job are hidden (the job can change while the page is open).
    function menuHtml(kind, current) {
        const links = PAGES.map((p) => {
            const on = p.id === current;
            return `<button type="button" class="pl-page-link${on ? " on" : ""}" data-act="page" data-page="${p.id}" role="tab" aria-selected="${on ? "true" : "false"}"${fits(p.id, kind) ? "" : " hidden"}>${pageTitle(p.id, kind)}</button>`;
        });
        return `<nav class="pl-page-menu" role="tablist" aria-label="Camera settings">${links.join("")}</nav>`;
    }

    // The camera page open before Line setup was drawn again, when it is still this line's camera.
    function reopen(open, lineId, cameraIds) {
        return open && open.lineId === lineId && cameraIds.includes(open.id) ? open : null;
    }

    const api = { PAGES, fits, pagesFor, pageTitle, pageToShow, pageAfterJobChange, startPage, pageForProblem, summary, menuHtml, reopen };
    root.CameraPages = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
