/**
 * A tiny, hand-rolled fake DOM - just enough of the Element/Document surface that
 * ./dom.js's `el()`/`safeLink()`/`clear()` use - so XSS-safety and DOM-construction
 * behavior can be proven in plain Node without pulling in jsdom (no new dependency).
 */

class FakeNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this._text = "";
    this.className = "";
    this.attributes = {};
    this.children = [];
    this._listeners = {};
  }

  set textContent(value) {
    this._text = value;
    // A real DOM would drop any existing child nodes when textContent is set - mirror
    // that so a test can prove text is never parsed as markup/children.
    this.children = [];
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((c) => c.textContent).join("");
  }

  get firstChild() {
    return this.children[0] || null;
  }

  setAttribute(key, value) {
    this.attributes[key] = String(value);
  }

  getAttribute(key) {
    return this.attributes[key] ?? null;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
  }

  addEventListener(type, handler) {
    this._listeners[type] = this._listeners[type] || [];
    this._listeners[type].push(handler);
  }

  dispatch(type) {
    for (const handler of this._listeners[type] || []) handler();
  }

  /** Mimics innerHTML's serialization ONLY for test assertions - never used by dom.js
   * itself. Lets a test prove that a string like "<script>" never becomes a real tag. */
  serialize() {
    const attrs = Object.entries(this.attributes)
      .map(([k, v]) => ` ${k}="${v}"`)
      .join("");
    const inner = this.children.length
      ? this.children.map((c) => c.serialize()).join("")
      : escapeForAssertionOnly(this._text);
    return `<${this.tagName.toLowerCase()}${attrs}>${inner}</${this.tagName.toLowerCase()}>`;
  }
}

function escapeForAssertionOnly(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

export function createFakeDocument() {
  return {
    createElement(tag) {
      return new FakeNode(tag);
    },
  };
}
