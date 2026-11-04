// Drives pattern_check.js's first Reference drag on a held picture with a tiny fake DOM (from the whole-branch
// review); prints what was drawn and shown. Run by tests/test_pattern_check_page.py.
const path = process.argv[2];
const strokes = [];
const ctx = { clearRect() {}, save() {}, restore() {}, setLineDash() {}, strokeRect(...a) { strokes.push(a); }, fillText() {},
  drawImage() {}, beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, stroke() {},
  createImageData(w, h) { return { data: new Uint8Array(w * h * 4) }; }, putImageData() {}, getImageData(w, h) { return { data: new Uint8Array(4) }; } };
const registry = {};
function el(name) {
  const cls = new Set();
  const e = { name, hidden: false, dataset: {}, value: "", textContent: "", innerHTML: "", style: {}, className: "", disabled: false,
    isConnected: true, clientWidth: 640, clientHeight: 480, naturalWidth: 1280, naturalHeight: 960, listeners: {},
    classList: { toggle(c, on) { if (on) cls.add(c); else cls.delete(c); }, contains(c) { return cls.has(c); }, add(c) { cls.add(c); } },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    dispatchEvent(ev) { (this.listeners[ev.type] || []).forEach((f) => f(ev)); return true; },
    querySelector(s) { return get(s); }, querySelectorAll(s) { return s === "[data-pc-check]" ? [row] : s === "img" ? [] : []; },
    closest() { return null; }, contains() { return true; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 480 }; }, setPointerCapture() {}, getContext() { return ctx; } };
  return e;
}
function get(s) { return registry[s] || (registry[s] = el(s)); }
const row = el("row");
row.querySelector = (s) => { const k = "row " + s; if (!registry[k]) { registry[k] = el(k); registry[k].value = s.includes("name") ? "Main" : s.includes("colour") ? "grey" : s.includes("sensitivity") ? "normal" : "80"; } return registry[k]; };
const img = get("img");
Object.defineProperty(img, "src", { set(v) { setTimeout(() => img.onload && img.onload(), 0); } });
globalThis.document = { hidden: false, createElement: () => el("canvas") };
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.URL.createObjectURL = () => "blob:x"; globalThis.URL.revokeObjectURL = () => {};
const PC = require(path);
const card = el("card"); card.dataset.camera = "cam1"; card.querySelector = get;
const pic = get('[data-f="pc-pic"]'); pic.querySelector = get;
let serverDraft = null;
const api = {
  fetchJson: async (p, o = {}) => {
    if (p.endsWith("/draft/camera/cam1") && (!o.method || o.method === "GET")) { if (!serverDraft) throw new Error("No draft"); return serverDraft; }
    if (p.endsWith("/draft/camera/cam1") && o.method === "PUT") { serverDraft = { reference: o.body.reference, checks: [], search_area: null, samples: [{ n: 1, score: 100 }], trained: null, picture_size: [1280, 960] }; return serverDraft; }
    throw new Error("unexpected " + p);
  },
  fetchBlob: async () => ({}), sendBlob: async () => ({}), entry: () => ({}), active: () => true, readOnly: false,
};
const tick = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  PC.bind(card, api);
  await tick();
  get('[data-act="pc-hold"]').dataset.act = "pc-hold";
  get(".pc-body").dispatchEvent({ type: "click", target: { closest: () => get('[data-act="pc-hold"]') } });
  await tick(); await tick();
  const ev = (x, y) => ({ clientX: x, clientY: y, pointerId: 1, preventDefault() {} });
  strokes.length = 0;
  pic.dispatchEvent({ type: "pointerdown", ...ev(200, 150) });
  pic.dispatchEvent({ type: "pointermove", ...ev(300, 250) });
  pic.dispatchEvent({ type: "pointermove", ...ev(400, 320) });
  const duringDrag = strokes.length;
  pic.dispatchEvent({ type: "pointerup", ...ev(400, 320) });
  await tick(); await tick(); await tick();
  console.log(JSON.stringify({ serverDraftStarted: !!serverDraft, boxesDrawnDuringDrag: duringDrag, boxesDrawnAfter: strokes.length - duringDrag,
    trainingButtonHidden: get('[data-act="pc-training"]').hidden, note: get('[data-f="pc-result"]').textContent }));
  process.exit(0);
})();
