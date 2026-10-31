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

// ---- Searchable dropdowns -------------------------------------------------------
// Any <select class="select"> with several options becomes a type-to-search box. The real select
// stays in the form, hidden, so posting, htmx, Alpine and onchange all work unchanged.
// Add data-native to keep a plain dropdown.
(() => {
  const MIN_OPTIONS = 6;

  function enhance(select) {
    if (select.dataset.sdSearch || select.hasAttribute("data-native") || select.multiple) return;
    if (select.options.length < MIN_OPTIONS && !select.hasAttribute("data-searchable")) return;
    select.dataset.sdSearch = "1";

    const box = document.createElement("div");
    box.className = "relative";
    const input = document.createElement("input");
    input.type = "text";
    input.className = select.className.replace(/\bselect\b/, "input");
    input.autocomplete = "off";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    const label = select.id && document.querySelector(`label[for="${CSS.escape(select.id)}"]`);
    input.setAttribute(
      "aria-label",
      (label && label.textContent.trim()) || select.getAttribute("aria-label") || "Search"
    );
    if (select.id) {
      if (label) label.setAttribute("for", `${select.id}-search`);
      input.id = `${select.id}-search`;
    }
    const list = document.createElement("ul");
    list.setAttribute("role", "listbox");
    list.className =
      "absolute z-30 mt-1 max-h-64 w-full min-w-[14rem] overflow-y-auto rounded-card border border-line bg-white shadow-lg";
    list.hidden = true;

    select.after(box);
    box.append(input, list);
    box.append(select);
    select.hidden = true;
    select.tabIndex = -1;
    select.required = false; // a hidden field cannot show the browser's message; the server checks

    let active = -1;
    let shown = [];
    let creating = "";
    const current = () => select.options[select.selectedIndex];
    const showCurrent = () => {
      const opt = current();
      input.value = opt && opt.value ? opt.textContent.trim() : "";
      input.placeholder = opt && !opt.value ? opt.textContent.trim() : "Type to search…";
    };
    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      showCurrent();
    };
    const mark = () => {
      [...list.children].forEach((li, i) => {
        li.setAttribute("aria-selected", i === active ? "true" : "false");
        li.classList.toggle("bg-mint", i === active);
      });
      if (list.children[active]) list.children[active].scrollIntoView({ block: "nearest" });
    };
    const render = (query) => {
      const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
      shown = [...select.options].filter((o) => {
        const text = o.textContent.toLowerCase();
        return !o.disabled && words.every((w) => text.includes(w));
      });
      list.replaceChildren(
        ...shown.slice(0, 100).map((o, i) => {
          const li = document.createElement("li");
          li.setAttribute("role", "option");
          li.className = "cursor-pointer px-3 py-2 text-body hover:bg-mint";
          li.textContent = o.textContent.trim();
          li.addEventListener("mousedown", (e) => {
            e.preventDefault();
            choose(i);
          });
          return li;
        })
      );
      if (!shown.length) {
        const li = document.createElement("li");
        li.className = "px-3 py-2 text-label text-muted";
        li.textContent = "No match";
        list.append(li);
      }
      creating = "";
      const typed = query.trim();
      const exact = shown.some((o) => o.textContent.trim().toLowerCase() === typed.toLowerCase());
      if (select.dataset.createUrl && typed.length >= 2 && !exact) {
        creating = typed;
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.className = "cursor-pointer border-t border-line px-3 py-2 text-body font-medium text-teal-deep hover:bg-mint";
        li.textContent = `+ Add "${typed}"`;
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          create();
        });
        list.querySelector("li:not([role])")?.remove();
        list.append(li);
      }
      active = shown.length ? 0 : -1;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      mark();
    };
    const moveOn = () => {
      if (select.dataset.next) document.querySelector(select.dataset.next)?.focus();
    };
    const choose = (i) => {
      const opt = shown[i];
      if (!opt) return;
      const changed = select.value !== opt.value;
      select.value = opt.value;
      close();
      if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
      moveOn();
    };
    const create = async () => {
      const token = document.querySelector("[name=csrfmiddlewaretoken]");
      const response = await fetch(select.dataset.createUrl, {
        method: "POST",
        body: new URLSearchParams({ name: creating, csrfmiddlewaretoken: token ? token.value : "" }),
      });
      const data = await response.json();
      if (!response.ok) {
        window.dispatchEvent(new CustomEvent("toast", { detail: { level: "danger", message: data.error } }));
        return;
      }
      select.add(new Option(data.name, data.id, true, true));
      close();
      select.dispatchEvent(new Event("change", { bubbles: true }));
      moveOn();
    };

    input.addEventListener("focus", () => {
      input.select();
      render("");
    });
    input.addEventListener("input", () => render(input.value));
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (list.hidden) render("");
        const last = Math.min(shown.length, 100) - 1;
        active = e.key === "ArrowDown" ? Math.min(active + 1, last) : Math.max(active - 1, 0);
        mark();
      } else if (e.key === "Enter" && !list.hidden) {
        e.preventDefault();
        if (active < 0 && creating) create();
        else choose(active);
      } else if (e.key === "Escape" && !list.hidden) {
        e.stopPropagation();
        close();
      }
    });
    // Options or value changed by htmx or Alpine: keep the visible text right.
    select.addEventListener("change", showCurrent);
    new MutationObserver(showCurrent).observe(select, { childList: true });
    showCurrent();
    if (select.hasAttribute("data-autofocus")) input.focus();
  }

  function scan(root) {
    if (root.matches && root.matches("select.select")) enhance(root);
    if (root.querySelectorAll) root.querySelectorAll("select.select").forEach(enhance);
  }

  document.addEventListener("DOMContentLoaded", () => {
    // Alpine fills x-model selects after load, so give it a tick before the first scan.
    setTimeout(() => {
      scan(document.body);
      new MutationObserver((changes) => {
        changes.forEach((c) => c.addedNodes.forEach(scan));
      }).observe(document.body, { childList: true, subtree: true });
    }, 0);
  });
})();
