/*
 * SehatDesk UI behaviour: Alpine components and htmx wiring.
 * Loaded before Alpine so the `alpine:init` listeners below register in time.
 * UI state only. Authorization always happens on the server.
 */

document.addEventListener("alpine:init", () => {
  // ---- Toasts ---------------------------------------------------------------
  // Fire from anywhere:   window.dispatchEvent(new CustomEvent("toast", {detail: {...}}))
  // Fire from the server: HX-Trigger: {"toast": {"level": "success", "message": "Saved"}}
  Alpine.store("toasts", {
    items: [],
    nextId: 1,
    push({ level = "info", message = "", timeout = 5000 } = {}) {
      const id = this.nextId++;
      this.items.push({ id, level, message });
      if (timeout) setTimeout(() => this.dismiss(id), timeout);
    },
    dismiss(id) {
      this.items = this.items.filter((t) => t.id !== id);
    },
  });

  // ---- Confirmation dialog ----------------------------------------------------
  // Replaces the browser confirm() for every hx-confirm attribute.
  // Optional attributes on the triggering element:
  //   data-confirm-title, data-confirm-action (button text), data-confirm-danger="true"
  Alpine.store("confirm", {
    open: false,
    title: "",
    message: "",
    actionLabel: "Confirm",
    danger: false,
    _resolve: null,
    ask({ title = "Are you sure?", message = "", actionLabel = "Confirm", danger = false }) {
      Object.assign(this, { title, message, actionLabel, danger, open: true });
      return new Promise((resolve) => (this._resolve = resolve));
    },
    answer(value) {
      this.open = false;
      if (this._resolve) this._resolve(value);
      this._resolve = null;
    },
  });

  // ---- Modal / drawer ----------------------------------------------------------
  // <div x-data="overlay('patient-form')"> ... </div>
  // Open with: $dispatch('open-overlay', 'patient-form'). Close with Esc, backdrop or close().
  Alpine.data("overlay", (name) => ({
    name,
    open: false,
    init() {
      window.addEventListener("open-overlay", (e) => {
        if (e.detail === this.name) this.open = true;
      });
      window.addEventListener("close-overlay", (e) => {
        if (!e.detail || e.detail === this.name) this.open = false;
      });
    },
    close() {
      this.open = false;
    },
  }));

  // ---- Tabs --------------------------------------------------------------------
  Alpine.data("tabs", (initial) => ({
    active: initial,
    select(name) {
      this.active = name;
    },
    isActive(name) {
      return this.active === name;
    },
  }));
});

// ---- htmx wiring ----------------------------------------------------------------
document.addEventListener("htmx:confirm", (event) => {
  const question = event.detail.question;
  if (!question) return;
  event.preventDefault();
  const el = event.detail.elt;
  Alpine.store("confirm")
    .ask({
      title: el.dataset.confirmTitle || "Please confirm",
      message: question,
      actionLabel: el.dataset.confirmAction || "Confirm",
      danger: el.dataset.confirmDanger === "true",
    })
    .then((ok) => {
      if (ok) event.detail.issueRequest(true);
    });
});

window.addEventListener("toast", (event) => Alpine.store("toasts").push(event.detail));

// Server errors on htmx requests surface as a toast instead of failing silently.
document.addEventListener("htmx:responseError", (event) => {
  const status = event.detail.xhr.status;
  const message =
    status === 403
      ? "You do not have permission to do that."
      : "Something went wrong. Please try again.";
  Alpine.store("toasts").push({ level: "danger", message });
});
document.addEventListener("htmx:sendError", () => {
  Alpine.store("toasts").push({
    level: "danger",
    message: "Cannot reach the SehatDesk server. Check the network connection.",
  });
});

// Plain (non-htmx) forms: put data-confirm="message" on the submit button or the form.
// Same optional data-confirm-title / -action / -danger attributes as hx-confirm.
document.addEventListener("submit", (event) => {
  const form = event.target;
  const source = event.submitter && event.submitter.dataset.confirm ? event.submitter : form;
  const message = source.dataset.confirm;
  if (!message || form.dataset.confirmed === "1") return;
  event.preventDefault();
  Alpine.store("confirm")
    .ask({
      title: source.dataset.confirmTitle || "Please confirm",
      message,
      actionLabel: source.dataset.confirmAction || "Confirm",
      danger: source.dataset.confirmDanger === "true",
    })
    .then((ok) => {
      if (!ok) return;
      form.dataset.confirmed = "1";
      form.requestSubmit(event.submitter || undefined);
    });
});

// ---- Browser error reporting ------------------------------------------------------
// Sends script errors to the server (Settings → System) so problems on staff machines are
// visible. Only the error text, script file and line, and the page path without its query
// string are sent; the server masks identifiers again before storing. At most 5 per page.
(() => {
  let sent = 0;
  const report = (type, message, source, line, column) => {
    if (sent >= 5 || !navigator.sendBeacon) return;
    sent += 1;
    const payload = JSON.stringify({
      type: String(type || "Error").slice(0, 100),
      message: String(message || "").slice(0, 300),
      source: String(source || "").split("?")[0].slice(0, 200),
      line,
      column,
      page: window.location.pathname,
    });
    navigator.sendBeacon("/telemetry/client-error/", new Blob([payload], { type: "application/json" }));
  };
  window.addEventListener("error", (e) => {
    if (!e.error && !e.message) return; // resource load errors carry no script error
    report(e.error && e.error.name, e.message, e.filename, e.lineno, e.colno);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason || {};
    report(r.name || "UnhandledRejection", r.message || String(e.reason), "", "", "");
  });
})();
