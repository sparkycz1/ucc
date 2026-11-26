// The header's activity indicator (`app.web.routes.activity`): polls
// `/activity` — every 3 s while something runs, every 15 s otherwise —
// and shows what runs. When everything is done it says so, and on a page
// that asks for it (`<body data-refresh-on-idle>`) re-fetches the page's
// own content in place, so a finished sync, catalogue refresh or
// assessment shows up without a reload.
(() => {
  const slot = document.getElementById("activity");
  if (!slot) return;
  let lastBusy = null;
  let timer = null;

  function toast(text) {
    if (!text) return;
    const el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.classList.add("toast-hide"), 3500);
    setTimeout(() => el.remove(), 4200);
  }

  async function refreshMain() {
    // Drop "?queued=…"/"?refreshing=1": that notice is now out of date.
    const url = window.location.pathname + window.location.hash;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      const fresh = doc.querySelector("main");
      const current = document.querySelector("main");
      if (!fresh || !current) return;
      current.innerHTML = fresh.innerHTML;
      if (window.history.replaceState) window.history.replaceState(null, "", url);
      if (window.htmx) window.htmx.process(current);
    } catch {
      // Offline for a moment — the next poll tries again.
    }
  }

  async function poll() {
    let busy = lastBusy ?? 0;
    try {
      const response = await fetch("/activity", { credentials: "same-origin", cache: "no-store" });
      if (response.ok) {
        slot.innerHTML = await response.text();
        const state = slot.querySelector("[data-busy]");
        busy = state ? Number(state.getAttribute("data-busy")) || 0 : 0;
      }
    } catch {
      // Keep the last state.
    }
    // Opened right after starting something ("?refreshing=1", "?queued=…")
    // and it's already done: same as watching it finish.
    const justStarted = lastBusy === null && /[?&](refreshing|queued|sync)=/.test(window.location.search);
    if ((lastBusy || justStarted) && busy === 0) {
      toast(slot.getAttribute("data-done-text"));
      if (document.body.hasAttribute("data-refresh-on-idle")) refreshMain();
    }
    lastBusy = busy;
    timer = setTimeout(poll, busy ? 3000 : 15000);
  }

  // A form that starts background work (busy.js) asks for a quick look.
  document.addEventListener("ucc:work-started", () => {
    clearTimeout(timer);
    timer = setTimeout(poll, 1000);
  });

  poll();
})();
