// Instant tooltips for every `title` on the page (the browser's own appear
// only after a delay, and never on a touch screen — so a badge like "GA"
// looked like it explained nothing). On hover, keyboard focus or a tap,
// the element's title is shown in a floating box positioned against the
// viewport (`position: fixed`), so a table's own scroll box can't clip it.
// The title moves to `data-tip` on first use so the browser doesn't show
// its own copy as well; `aria-description` keeps it for screen readers.
(() => {
  let box = null;
  let owner = null;

  function textOf(el) {
    if (el.hasAttribute("title")) {
      const text = el.getAttribute("title");
      el.removeAttribute("title");
      if (text) {
        el.setAttribute("data-tip", text);
        if (!el.hasAttribute("aria-description")) el.setAttribute("aria-description", text);
      }
    }
    return el.getAttribute("data-tip");
  }

  function hide() {
    if (box) box.remove();
    box = null;
    owner = null;
  }

  function show(el) {
    const text = textOf(el);
    if (!text) return;
    if (owner === el) return;
    hide();
    owner = el;
    box = document.createElement("div");
    box.className = "ucc-tooltip";
    box.setAttribute("role", "tooltip");
    box.textContent = text;
    document.body.appendChild(box);
    const r = el.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    const gap = 6;
    let top = r.bottom + gap;
    if (top + b.height > window.innerHeight - 4) top = Math.max(4, r.top - b.height - gap);
    let left = r.left + r.width / 2 - b.width / 2;
    left = Math.max(4, Math.min(left, window.innerWidth - b.width - 4));
    box.style.top = `${top}px`;
    box.style.left = `${left}px`;
  }

  function target(event) {
    const el = event.target instanceof Element ? event.target.closest("[title], [data-tip]") : null;
    if (!el || el.closest("select, option")) return null;
    return el;
  }

  document.addEventListener("mouseover", (event) => {
    const el = target(event);
    if (el) show(el);
    else if (owner && !(event.target instanceof Node && owner.contains(event.target))) hide();
  });
  document.addEventListener("focusin", (event) => {
    const el = target(event);
    if (el) show(el);
  });
  document.addEventListener("focusout", hide);
  // A tap on something that isn't a link or a control shows its tip.
  document.addEventListener("click", (event) => {
    const el = target(event);
    if (!el) return hide();
    if (el.closest("a, button, input, label, summary")) return;
    if (owner === el) hide();
    else show(el);
  });
  window.addEventListener("scroll", hide, { passive: true, capture: true });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hide();
  });
})();
