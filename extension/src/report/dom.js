/**
 * Minimal, dependency-free safe DOM construction helper (Part 6B-C).
 *
 * NEVER uses innerHTML/outerHTML/insertAdjacentHTML anywhere. Every text value is
 * assigned via `textContent`, so untrusted report/document text - including something
 * like `<script>alert(1)</script>` retained verbatim from a source document - can never
 * become executable markup. Every higher-level report-rendering function is built on
 * top of this one primitive, so this file is the single place that needs to be (and is)
 * tested for XSS-safety; everything built from it inherits the guarantee.
 *
 * `doc` is passed explicitly (defaulting to the real global `document` in the
 * extension) so this module is testable in plain Node with a tiny fake document -
 * no jsdom, no external dependency.
 */

/**
 * @param {Document} doc
 * @param {string} tag
 * @param {{text?: string, className?: string, attrs?: Record<string,string>, children?: (Node|null|undefined)[]}} [options]
 */
export function el(doc, tag, options = {}) {
  const node = doc.createElement(tag);
  if (options.text !== undefined && options.text !== null) {
    node.textContent = String(options.text);
  }
  if (options.className) {
    node.className = options.className;
  }
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
  }
  for (const child of options.children || []) {
    if (child) node.appendChild(child);
  }
  return node;
}

/** A safe <a> that only ever gets a real http(s) href - never javascript:/data:/file:. */
export function safeLink(doc, { href, text, className }) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    return null;
  }
  return el(doc, "a", {
    text: text ?? href,
    className,
    attrs: { href: url.toString(), target: "_blank", rel: "noopener noreferrer" },
  });
}

/** Removes all children of a node without ever touching innerHTML. */
export function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}
