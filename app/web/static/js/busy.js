// Every form shows at once that its click was taken: the submit button
// gets a spinner and "working…", and a second click does nothing until the
// page answers (no accidental double "Assess all"). Runs after confirm.js,
// so a cancelled confirmation leaves the button alone. A form that answers
// with a download (no navigation) resets itself after a few seconds.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
  if (form.hasAttribute("data-no-busy")) return;
  if (form.dataset.submitting === "1") {
    event.preventDefault();
    return;
  }
  form.dataset.submitting = "1";
  const button = event.submitter instanceof HTMLElement ? event.submitter : form.querySelector("[type=submit]");
  if (button) {
    button.classList.add("is-busy");
    button.setAttribute("aria-busy", "true");
  }
  document.dispatchEvent(new CustomEvent("ucc:work-started"));
  setTimeout(() => {
    delete form.dataset.submitting;
    if (button) {
      button.classList.remove("is-busy");
      button.removeAttribute("aria-busy");
    }
  }, 8000);
});
// Coming back with the browser's Back button restores the page as it was —
// busy buttons included; clear them.
window.addEventListener("pageshow", () => {
  document.querySelectorAll(".is-busy").forEach((el) => {
    el.classList.remove("is-busy");
    el.removeAttribute("aria-busy");
  });
  document.querySelectorAll("form[data-submitting]").forEach((form) => delete form.dataset.submitting);
});
