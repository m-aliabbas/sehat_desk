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

  // ---- Ticking rows ------------------------------------------------------------
  // <section x-data="sdPick"> around a table whose rows tick <input name="ids">; `n` counts them
  // for a "Delete selected" bar (recyclebin/_select_bar.html), approvals and the Recycle bin.
  Alpine.data("sdPick", () => ({
    n: 0,
    boxes() {
      return [...this.$root.querySelectorAll("input[name=ids]")];
    },
    count() {
      this.n = this.boxes().filter((b) => b.checked).length;
    },
    all(e) {
      this.boxes().forEach((b) => (b.checked = e.target.checked));
      this.count();
    },
    none() {
      this.boxes().forEach((b) => (b.checked = false));
      this.$root.querySelectorAll("thead input[type=checkbox]").forEach((b) => (b.checked = false));
      this.count();
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
// Every <select class="select"> becomes one kind of dropdown: a box you type in to search, with a
// list that floats above tables and cards. The real select stays in the form, hidden, so posting,
// htmx, Alpine and onchange all work unchanged. In a table the list opens on click, typing or
// Alt+Down, so arrow keys can walk the cells; elsewhere it also opens on focus.
(() => {
  function enhance(select) {
    if (select.dataset.sdSearch || select.hasAttribute("data-native") || select.multiple) return;
    select.dataset.sdSearch = "1";

    const box = document.createElement("div");
    box.className = "relative";
    const input = document.createElement("input");
    input.type = "text";
    input.className = select.className.replace(/\bselect\b/, "input");
    input.autocomplete = "off";
    input.dataset.sdCombo = "1";
    input.tabIndex = select.tabIndex;
    input.disabled = select.disabled;
    const inTable = () => Boolean(input.closest("table"));
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
      "combo-list";
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
    // A form field marked data-create-kind, for someone allowed to add to that list (base.html).
    const kind = select.dataset.createKind;
    const addable = (document.body.dataset.quickAdd || "").split(" ");
    const createUrl = select.dataset.createUrl ||
      (kind && addable.includes(kind) ? document.body.dataset.quickAddUrl.replace("KIND", kind) : "");
    const current = () => select.options[select.selectedIndex];
    const showCurrent = () => {
      const opt = current();
      input.value = opt && opt.value ? opt.textContent.trim() : "";
      input.placeholder = opt && !opt.value ? opt.textContent.trim() : "Type to search…";
    };
    select.sdShowCurrent = () => {
      if (document.activeElement !== input) showCurrent();
      input.disabled = select.disabled;
    };
    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      showCurrent();
    };
    const mark = () => {
      [...list.children].forEach((li, i) => {
        li.setAttribute("aria-selected", i === active ? "true" : "false");
      });
      if (list.children[active]) list.children[active].scrollIntoView({ block: "nearest" });
    };
    const place = () => {
      const rect = input.getBoundingClientRect();
      list.style.left = `${rect.left}px`;
      list.style.minWidth = `${Math.max(rect.width, 160)}px`;
      const below = window.innerHeight - rect.bottom > list.offsetHeight + 8;
      list.style.top = `${below ? rect.bottom + 2 : Math.max(rect.top - list.offsetHeight - 2, 8)}px`;
    };
    let openedAt = 0;
    const render = (query) => {
      openedAt = Date.now();
      const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
      shown = [...select.options].filter((o) => {
        const text = o.textContent.toLowerCase();
        return !o.disabled && words.every((w) => text.includes(w));
      });
      list.replaceChildren(
        ...shown.slice(0, 100).map((o, i) => {
          const li = document.createElement("li");
          li.setAttribute("role", "option");
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
        li.className = "text-muted";
        li.textContent = "No match";
        list.append(li);
      }
      creating = "";
      const typed = query.trim();
      const exact = shown.some((o) => o.textContent.trim().toLowerCase() === typed.toLowerCase());
      if (createUrl && typed.length >= 2 && !exact) {
        creating = typed;
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.className = "border-t border-line font-medium text-teal-deep";
        li.textContent = `+ Add "${typed}"`;
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          create();
        });
        list.querySelector("li:not([role])")?.remove();
        list.append(li);
      }
      // Nothing typed: start on the current value, so Enter keeps it and moves on.
      const at = words.length ? 0 : shown.indexOf(current());
      active = shown.length || creating ? Math.max(at, 0) : -1;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      place();
      mark();
    };
    const moveOn = () => {
      if (select.dataset.next) document.querySelector(select.dataset.next)?.focus();
      else if (input.closest("form:is([method=post], [hx-post]) .field")) sdNextBox(input)?.focus();
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
      const response = await fetch(createUrl, {
        method: "POST",
        body: new URLSearchParams({ name: creating, csrfmiddlewaretoken: token ? token.value : "" }),
      });
      const data = await response.json().catch(() => ({ error: "Could not add this." }));
      if (!response.ok) {
        window.dispatchEvent(new CustomEvent("toast", { detail: { level: "danger", message: data.error } }));
        return;
      }
      // Other boxes of the same list on this screen (rows of a grid) can pick it too.
      if (kind) {
        document.querySelectorAll(`select[data-create-kind="${kind}"]`).forEach((other) => {
          if (other !== select && ![...other.options].some((o) => o.value === String(data.id))) {
            other.add(new Option(data.name, data.id));
          }
        });
      }
      if ([...select.options].some((o) => o.value === String(data.id))) select.value = data.id;
      else select.add(new Option(data.name, data.id, true, true));
      close();
      select.dispatchEvent(new Event("change", { bubbles: true }));
      moveOn();
    };

    input.addEventListener("focus", () => {
      input.select();
      if (!inTable() && !window.sdQuietFocus) render("");
    });
    input.addEventListener("click", () => {
      if (list.hidden) render("");
    });
    window.addEventListener("scroll", (e) => {
      if (list.hidden || e.target === list) return;
      if (Date.now() - openedAt < 300) place(); // focus itself scrolled the box into view
      else close();
    }, true);
    input.addEventListener("input", () => render(input.value));
    input.addEventListener("blur", close);
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (list.hidden && inTable() && !e.altKey) return; // the sheet moves to the next cell
        e.preventDefault();
        if (list.hidden) {
          render("");
          return;
        }
        const last = Math.min(shown.length, 100) - (creating ? 0 : 1); // "+ Add" is the last row
        active = e.key === "ArrowDown" ? Math.min(active + 1, last) : Math.max(active - 1, 0);
        mark();
      } else if (e.key === "Enter" && !list.hidden) {
        e.preventDefault();
        if (creating && active === Math.min(shown.length, 100)) create();
        else choose(active);
      } else if (e.key === "Escape" && !list.hidden) {
        e.stopPropagation();
        close();
      }
    });
    // Options or value changed by htmx or Alpine: keep the visible text right.
    select.addEventListener("change", showCurrent);
    new MutationObserver(showCurrent).observe(select, { childList: true, subtree: true, characterData: true });
    new MutationObserver(() => select.sdShowCurrent()).observe(select, { attributes: true, attributeFilter: ["disabled"] });
    showCurrent();
    if (select.hasAttribute("data-autofocus")) input.focus();
  }

  function scan(root) {
    if (root.matches && root.matches("select.select")) enhance(root);
    if (root.querySelectorAll) root.querySelectorAll("select.select").forEach(enhance);
  }

  // Alpine sets x-model selects without any event: refresh the visible text after anything that could.
  let refresh = 0;
  ["click", "keyup", "change"].forEach((name) =>
    document.addEventListener(name, () => {
      clearTimeout(refresh);
      refresh = setTimeout(() => document.querySelectorAll("select[data-sd-search]").forEach((el) => el.sdShowCurrent()), 30);
    })
  );

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

// The next box after this one in its entry form; the save button after the last.
function sdNextBox(el, step = 1) {
  const form = el.closest("form");
  if (!form) return null;
  const boxes = [...form.querySelectorAll('input:not([type=hidden]):not([data-grid-filter]), select, textarea, button.btn-primary')].filter(
    (b) => b.offsetParent !== null && b.tabIndex >= 0 && !b.disabled && !b.closest("details:not([open])")
  );
  return boxes[boxes.indexOf(el) + step] || null;
}

// ---- Dropdown on a free-text box ---------------------------------------------------
// <input data-list="unit-names"> offers the options of that <datalist> and still takes any typed
// word. The list floats on the page, so a scrolling table cannot clip it. Opens on click, typing
// or Alt+Down, never on plain focus, so Enter and the arrow keys stay quick.
const sdCombo = (() => {
  let input = null;
  let items = [];
  let active = -1;
  const list = document.createElement("ul");
  list.setAttribute("role", "listbox");
  list.className = "combo-list";
  list.hidden = true;

  const isOpen = () => !list.hidden;
  const close = () => {
    list.hidden = true;
    if (input) input.setAttribute("aria-expanded", "false");
    input = null;
    active = -1;
  };
  const mark = () => {
    [...list.children].forEach((li, i) => {
      li.setAttribute("aria-selected", i === active ? "true" : "false");
      if (i === active) li.scrollIntoView({ block: "nearest" });
    });
  };
  const pick = (value) => {
    const target = input;
    target.value = value;
    target.dispatchEvent(new Event("input", { bubbles: true })); // Alpine x-model
    close();
  };
  const open = (box, filter) => {
    const source = document.getElementById(box.dataset.list);
    if (!source) return;
    const typed = filter ? box.value.trim().toLowerCase() : "";
    items = [...source.options].map((o) => o.value).filter((v) => v.toLowerCase().includes(typed));
    if (!items.length) return close();
    input = box;
    active = -1;
    list.replaceChildren(
      ...items.map((value) => {
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.textContent = value;
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          pick(value);
        });
        return li;
      })
    );
    if (!list.isConnected) document.body.append(list);
    list.hidden = false;
    box.setAttribute("aria-expanded", "true");
    const rect = box.getBoundingClientRect();
    list.style.left = `${rect.left}px`;
    list.style.minWidth = `${rect.width}px`;
    const below = window.innerHeight - rect.bottom > list.offsetHeight + 8;
    list.style.top = `${below ? rect.bottom + 2 : Math.max(rect.top - list.offsetHeight - 2, 8)}px`;
  };

  document.addEventListener("click", (e) => {
    if (e.target.matches?.("input[data-list]")) open(e.target, false);
  });
  document.addEventListener("input", (e) => {
    if (e.target.matches?.("input[data-list]") && e.isTrusted) open(e.target, true);
  });
  document.addEventListener("focusout", (e) => {
    if (e.target === input) close();
  });
  window.addEventListener("resize", close);
  document.addEventListener("scroll", (e) => {
    if (isOpen() && e.target !== list) close();
  }, true);

  // Returns true when the key belonged to the list, so nothing else should act on it.
  function handleKey(e) {
    if (e.key === "ArrowDown" && e.altKey && e.target.matches("input[data-list]")) {
      e.preventDefault();
      open(e.target, false);
      return true;
    }
    if (e.target !== input || !isOpen()) return false;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      active = Math.min(Math.max(active + step, 0), items.length - 1);
      mark();
      return true;
    }
    if (e.key === "Enter" && active >= 0) pick(items[active]); // Enter still moves to the next box
    else if (e.key === "Enter" || e.key === "Tab") close();
    else if (e.key === "Escape") {
      e.stopPropagation();
      close();
      return true;
    }
    return false;
  }
  return { handleKey };
})();

// ---- Tables: Excel-style keys, the row popup and shortcuts -----------------------------
// Every table.table and table.sheet inside #main gets this with no change to its template:
// - a last column with an expand button; Ctrl+E or the button opens the row in a popup.
//   Rows with boxes: the real boxes move into the popup and go back on close, so the form posts
//   and Alpine keeps working. Rows with a link: the linked page's content. Others: the row's values.
// - columns that do not fit are hidden and shown in the popup. Mark a column by hand with
//   class="cell-detail" on its th and td. data-plain on a table opts out of all of this.
// - arrow keys walk cells or rows; Ctrl+Delete removes a line. Actual rights stay on the server.
const sdTables = (() => {
  const TABLES = "#main table.table:not([data-plain]), #main table.sheet:not([data-plain])";
  const BOXES = "input:not([type=hidden]), select, textarea";
  const EXPAND_ICON =
    '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>';
  const icon = (paths) => `<svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
  const EDIT_ICON = icon('<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4Z"/>');
  const TRASH_ICON = icon('<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>');
  const SHORTCUTS = [
    ["Ctrl + E", "Open the current row with all its details"],
    ["Ctrl + Delete", "Remove the current line"],
    ["↑ ↓ ← →", "Move between rows and cells"],
    ["Enter", "Next box, or open the selected row"],
    ["E", "Edit the selected row"],
    ["Delete", "Remove or deactivate the selected row (asks first)"],
    ["F8 or Ctrl + S", "Save the form you are in"],
    ["Alt + N", "New or add, on the current page"],
    ["Alt + F", "Filter or search this list"],
    ["Alt + V", "First vitals box"],
    ["Alt + J / Alt + K", "Next or previous section of a form"],
    ["[  ]", "Previous or next page of a list"],
    ["Alt + ←", "Back to the previous page"],
    ["Alt + ↑", "Up one level, to the page above this one"],
    ["Alt + 1 to 9", "Open a menu item. Hold Alt to see the numbers"],
    ["/ or Ctrl + K", "Search patients and records, or type a screen's name to go there"],
    ["Ctrl + B", "Show or hide menu names"],
    ["Esc", "Close the popup"],
    ["? or Ctrl + /", "This list"],
  ];
  // Hold Alt: each menu item shows its number, so Alt + number needs no memorising.
  const menuNumbers = (on) => document.documentElement.classList.toggle("alt-held", on);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Alt") return;
    document.querySelectorAll(".sidebar-nav a.nav-link").forEach((a, i) => {
      if (i < 9) a.dataset.key = i + 1;
    });
    menuNumbers(true);
  });
  document.addEventListener("keyup", (e) => e.key === "Alt" && menuNumbers(false));
  window.addEventListener("blur", () => menuNumbers(false));
  const rows = (table) => [...table.tBodies].flatMap((b) => [...b.rows]).filter((r) => !isBlank(r));
  const isBlank = (tr) => tr.cells.length === 1 && tr.cells[0].colSpan > 1;
  const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
  const editable = (tr) => tr.querySelector(BOXES);
  const shown = (el) => el.offsetParent !== null;

  // ---- Setup ----
  function addRowCell(tr) {
    if (tr.dataset.sdRow || !tr.cells.length) return;
    tr.dataset.sdRow = "1";
    if (isBlank(tr)) {
      tr.cells[0].colSpan += 1;
      return;
    }
    const td = document.createElement("td");
    td.className = "row-expand";
    if (tr.parentElement.tagName === "TBODY") {
      // data-edit and data-remove are written by the template only for people allowed to use them.
      const edit = tr.dataset.edit ? `<a href="${tr.dataset.edit}" tabindex="-1" class="row-btn" title="Edit (E)" aria-label="Edit this row">${EDIT_ICON}</a>` : "";
      const remove = tr.dataset.remove ? `<button type="button" tabindex="-1" class="row-btn row-btn-danger" data-row-remove title="${tr.dataset.removeLabel || "Remove"} (Delete)" aria-label="${tr.dataset.removeLabel || "Remove"}">${TRASH_ICON}</button>` : "";
      td.innerHTML = `<div class="row-actions">${edit}${remove}<button type="button" tabindex="-1" class="row-btn" data-row-open title="Details (Ctrl+E)" aria-label="Show details of this row">${EXPAND_ICON}</button></div>`;
      if (!editable(tr)) tr.tabIndex = -1;
    }
    tr.append(td);
  }
  function prepare(table) {
    if (table.dataset.sdTable) return;
    table.dataset.sdTable = "1";
    // A table with no scrolling parent would push the whole page sideways on a small screen.
    if (!table.closest(".table-wrap, .sheet-wrap, .overflow-x-auto")) {
      const box = document.createElement("div");
      box.className = "table-wrap";
      table.before(box);
      box.append(table);
    }
    addFilter(table);
    table.tHead?.querySelectorAll("tr").forEach((tr) => {
      const th = document.createElement("th");
      th.className = "row-expand";
      th.innerHTML = '<span class="sr-only">Details</span>';
      tr.append(th);
    });
    table.querySelector("colgroup")?.append(Object.assign(document.createElement("col"), { style: "width: 2.25rem" }));
    table.querySelectorAll("tbody tr, tfoot tr").forEach(addRowCell);
    const first = rows(table).find((r) => r.tabIndex === -1);
    if (first) first.tabIndex = 0;
    oneTabStop(table);
    quietChips(table);
    markRowLinks(table);
    fit(table);
    const wrap = holder(table);
    let width = wrap.clientWidth;
    new ResizeObserver(() => {
      if (wrap.clientWidth !== width) {
        width = wrap.clientWidth;
        requestAnimationFrame(() => fit(table)); // not inside the callback: that logs a browser error
      }
    }).observe(wrap);
  }
  const holder = (table) => table.closest(".table-wrap, .sheet-wrap") || table.parentElement;

  // A long list with no search or pages gets a filter box. It only hides rows, so hidden boxes still post.
  function addFilter(table) {
    if (rows(table).length < 12 || document.querySelector('#main input[type=search], #main input[name=q], #main nav[aria-label="Pagination"]')) return;
    const box = Object.assign(document.createElement("input"), { type: "search", className: "input mb-3 max-w-xs", placeholder: "Filter this list" });
    box.dataset.gridFilter = "";
    box.setAttribute("aria-label", "Filter this list");
    box.addEventListener("input", () => {
      const words = clean(box.value).toLowerCase();
      let group = null;
      [...table.tBodies].flatMap((b) => [...b.rows]).forEach((tr) => {
        if (isBlank(tr)) {
          group = tr;
          tr.hidden = Boolean(words);
          return;
        }
        tr.hidden = !clean(tr.textContent).toLowerCase().includes(words);
        if (!tr.hidden && group) group.hidden = false;
      });
    });
    box.addEventListener("change", (e) => e.stopPropagation()); // not an edit to the form around it
    box.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const tr = rows(table).find(shown);
      (tr?.querySelector(BOXES) || tr)?.focus();
    });
    holder(table).before(box);
  }
  // A grid of tick boxes is one Tab stop; arrow keys walk it, and the next Tab leaves it.
  function oneTabStop(table) {
    const ticks = [...table.querySelectorAll('tbody input[type=checkbox]:not(:disabled)')];
    if (ticks.length < 50) return;
    ticks.forEach((t, i) => (t.tabIndex = i ? -1 : 0));
    table.addEventListener("focusin", (e) => {
      if (!ticks.includes(e.target)) return;
      ticks.forEach((t) => (t.tabIndex = t === e.target ? 0 : -1));
    });
  }

  // A status most rows share says nothing: it turns into plain words, and only exceptions keep their colour.
  function quietChips(table) {
    const body = rows(table);
    if (body.length < 5) return;
    const byColumn = new Map();
    body.forEach((tr) => {
      [...tr.cells].forEach((td, col) => {
        td.querySelectorAll(".chip").forEach((chip) => {
          if (!byColumn.has(col)) byColumn.set(col, new Map());
          const same = byColumn.get(col);
          const text = clean(chip.textContent);
          same.set(text, [...(same.get(text) || []), chip]);
        });
      });
    });
    byColumn.forEach((same) => {
      const chips = [...same.values()].flat();
      same.forEach((group) => {
        if (group.length / chips.length < 0.6 || group.length / body.length < 0.6) return;
        group.filter((c) => !c.matches(".chip-warning, .chip-danger")).forEach((c) => c.classList.add("chip-quiet"));
      });
    });
  }
  // Clicking anywhere on a read-only row opens it; its "Open" word then only shows on the row you point at.
  function markRowLinks(table) {
    rows(table).forEach((tr) => {
      if (editable(tr) || !mainLink(tr)) return;
      tr.dataset.rowLink = "1";
      [...tr.querySelectorAll("a")].filter((a) => clean(a.textContent) === "Open").forEach((a) => a.classList.add("row-open-link"));
    });
  }

  // Hide columns that do not fit: first and last data columns and actions go last or never.
  function fit(table) {
    const head = table.tHead?.rows[0];
    // Entry tables scroll instead: a box must never vanish because the window is narrow.
    if (table.classList.contains("sheet") || table.querySelector('tbody input:not([type=hidden]):not([type=checkbox]), tbody select, tbody textarea')) {
      table.querySelectorAll(".cell-fit-hidden").forEach((c) => c.classList.remove("cell-fit-hidden"));
      return;
    }
    if (!head || !shown(table) || [...head.cells].some((c) => c.colSpan > 1)) return;
    const all = [...table.querySelectorAll("tr")].filter((r) => !isBlank(r) && r.cells.length === head.cells.length);
    const setHidden = (col, on) => all.forEach((r) => r.cells[col].classList.toggle("cell-fit-hidden", on));
    [...head.cells].forEach((_, col) => setHidden(col, false));
    const data = [...head.cells]
      .map((c, col) => ({ c, col }))
      .filter(({ c }) => clean(c.textContent) && !c.classList.contains("row-expand") && !c.querySelector(".sr-only") && !c.classList.contains("cell-detail") && !c.classList.contains("sheet-freeze"))
      .map(({ col }) => col);
    const queue = [...data.slice(2, -1).reverse(), ...data.slice(-1).filter((c) => data.indexOf(c) > 1)];
    const wrap = holder(table);
    for (const col of queue) {
      if (table.offsetWidth <= wrap.clientWidth) break;
      setHidden(col, true);
    }
  }

  function scan(node) {
    if (node.nodeType !== 1) return;
    if (node.matches("tbody tr, tfoot tr") && node.closest("table[data-sd-table]")) {
      addRowCell(node);
      fit(node.closest("table"));
    }
    if (node.matches(TABLES)) prepare(node);
    node.querySelectorAll?.(TABLES).forEach(prepare);
    node.querySelectorAll?.("table[data-sd-table] tbody tr, table[data-sd-table] tfoot tr").forEach(addRowCell);
  }

  // ---- Popup ----
  let modal = null;
  function openPanel(title, wide, side) {
    closeModal();
    const box = document.createElement("div");
    box.className = side ? "fixed inset-0 z-50 flex justify-end" : "fixed inset-0 z-50 flex items-center justify-center p-4";
    box.innerHTML = `<div class="overlay" data-close></div>
      <div class="modal-panel z-50 flex flex-col ${side ? "panel-side" : `max-h-[90vh] ${wide ? "max-w-5xl" : "max-w-2xl"}`}" role="dialog" aria-modal="true" aria-label="Details">
        <div class="mb-4 flex items-start justify-between gap-4"><h2 class="section-title"></h2>
          <div class="flex shrink-0 items-center gap-2" data-tools><button type="button" class="btn btn-ghost btn-icon btn-sm" data-close aria-label="Close">✕</button></div></div>
        <div class="row-modal-body min-h-0 flex-1 overflow-y-auto" tabindex="-1"></div></div>`;
    box.querySelector("h2").textContent = title;
    box.addEventListener("click", (e) => {
      if (e.target.closest("[data-close]")) askClose();
    });
    // Before #overlay-root so popups opened from inside it still land on top.
    const root = document.getElementById("overlay-root");
    root ? root.before(box) : document.body.append(box);
    modal = { box, body: box.querySelector(".row-modal-body"), moved: [], from: document.activeElement };
    return modal;
  }
  function closeModal() {
    if (!modal) return;
    const { box, moved, from } = modal;
    moved.forEach(({ td, nodes }) => td.append(...nodes));
    modal = null;
    box.remove();
    if (from?.isConnected && shown(from)) from.focus();
  }
  // Typed work in the side panel is not thrown away by a stray Esc or click outside.
  function askClose() {
    if (!modal?.dirty) return closeModal();
    const m = modal;
    return Alpine.store("confirm")
      .ask({ title: "Discard what you typed?", message: "This form has not been saved.", actionLabel: "Discard", danger: true })
      .then((ok) => {
        if (ok && modal === m) closeModal();
      });
  }
  // A cell's words without its buttons, such as "+ batch".
  function cellText(el) {
    const copy = el.cloneNode(true);
    copy.querySelectorAll("button").forEach((b) => b.remove());
    return clean(copy.textContent);
  }
  function rowTitle(tr) {
    const first = tr.cells[0];
    const box = first.querySelector("input:not([type=hidden])");
    const shownText = first.querySelector(":scope > :not(input, label.sr-only)") || first;
    return (box && box.value) || cellText(shownText.querySelector(":scope > :not(button)") || shownText).slice(0, 80) || "Details";
  }
  function headerFor(tr) {
    const out = [];
    [...(tr.closest("table").tHead?.rows[0]?.cells || [])].forEach((c) => {
      for (let i = 0; i < c.colSpan; i += 1) out.push(c);
    });
    return out;
  }
  function field(label, nodes) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const name = document.createElement("span");
    name.className = "field-label";
    name.textContent = label;
    wrap.append(name, ...nodes);
    return wrap;
  }
  function openFields(tr) {
    const m = openPanel(rowTitle(tr));
    const grid = document.createElement("div");
    grid.className = "grid gap-4 sm:grid-cols-2";
    const heads = headerFor(tr);
    let col = 0;
    [...tr.cells].forEach((td) => {
      const head = heads[col];
      col += td.colSpan;
      if (td.classList.contains("row-expand")) return;
      const label = head ? clean(head.textContent) : "";
      if (td.querySelector(BOXES)) {
        const nodes = [...td.childNodes];
        m.moved.push({ td, nodes });
        grid.append(field(label || "Value", nodes));
      } else if (label && !head.querySelector(".sr-only")) {
        const value = document.createElement("span");
        value.className = "text-body";
        value.textContent = cellText(td) || "—";
        grid.append(field(label, [value]));
      }
    });
    m.body.append(grid);
    const boxes = [...m.body.querySelectorAll(BOXES)].filter(shown);
    (boxes[0] || m.body).focus();
  }
  function mainLink(tr) {
    return [...tr.querySelectorAll("a[href]")].find((a) => {
      const href = a.getAttribute("href");
      return href.startsWith("/") && !a.hasAttribute("download") && a.target !== "_blank" && !/print|pdf|csv|export/i.test(href);
    });
  }
  async function openLink(tr, link) {
    const m = openPanel(rowTitle(tr), true);
    m.body.innerHTML = '<div class="skeleton h-40"></div>';
    m.body.focus();
    const go = () => window.location.assign(link.href);
    try {
      const response = await fetch(link.href, { credentials: "same-origin" });
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const main = page.querySelector("#main");
      if (!response.ok || !main) return go();
      if (modal !== m) return undefined; // closed while loading
      main.querySelector("#page-trail")?.remove();
      m.body.replaceChildren(...main.childNodes);
      const open = document.createElement("a");
      open.href = link.href;
      open.className = "btn btn-secondary btn-sm";
      open.textContent = "Open full page";
      m.box.querySelector("[data-tools]").prepend(open);
      window.htmx?.process(m.body);
      setTimeout(() => sdFocusFirst(m.body), 60);
      m.body.querySelectorAll("table.table, table.sheet").forEach(prepareInPopup);
    } catch (e) {
      go();
    }
    return undefined;
  }
  // ---- Side panel: edit, collect and enter results beside the list, without leaving it ----
  // Links only need to match PANEL_PATHS or carry data-panel. Ctrl+click still opens the full page, and a
  // page the panel cannot host (its own inline script, or no #main) simply opens normally.
  const PANEL_PATHS = /\/(edit|pay|results)\/?$/;
  const panelWorthy = (a) => {
    if (!a?.getAttribute || a.target === "_blank" || a.hasAttribute("download") || a.dataset.panel === "off") return false;
    if (a.dataset.panel !== undefined || a.matches("a.row-btn")) return true;
    const href = a.getAttribute("href") || "";
    return href.startsWith("/") && PANEL_PATHS.test(href.split("?")[0]) && Boolean(a.closest("#main"));
  };
  function fillSide(m, html, url) {
    const page = new DOMParser().parseFromString(html, "text/html");
    const main = page.querySelector("#main");
    if (!main || main.querySelector("script:not([src]):not([type=\"application/json\"])")) {
      window.location.assign(url);
      return false;
    }
    main.querySelector("#page-trail")?.remove();
    main.querySelector('nav[aria-label="Settings sections"]')?.remove();
    const heading = main.querySelector("h1.page-title");
    if (heading?.textContent.trim()) m.box.querySelector("h2").textContent = heading.textContent.trim();
    heading?.remove();
    m.url = url;
    m.dirty = false;
    m.body.replaceChildren(...main.childNodes);
    window.htmx?.process(m.body);
    setTimeout(() => {
      sdFocusFirst(m.body);
      sdStickySave(m.body);
      const page = new URL(url);
      sdDrafts.attach(m.body, page.pathname + page.search);
    }, 60);
    return true;
  }
  async function openSide(link, title) {
    const m = openPanel(title || "Loading", true, true);
    m.body.innerHTML = '<div class="skeleton h-40"></div>';
    m.body.focus();
    const url = link.href;
    const tools = m.box.querySelector("[data-tools]");
    const full = Object.assign(document.createElement("a"), { href: url, className: "btn btn-secondary btn-sm", textContent: "Open full page" });
    full.dataset.panel = "off";
    tools.prepend(full);
    m.body.addEventListener("input", () => (m.dirty = true));
    m.body.addEventListener("click", (e) => {
      const a = e.target.closest("a[href]");
      if (a && /^(cancel|close|back)$/i.test(a.textContent.trim())) {
        e.preventDefault();
        askClose();
      }
    });
    m.body.addEventListener("submit", (e) => sideSubmit(e, m));
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (modal === m) {
        if (response.ok) fillSide(m, await response.text(), response.url);
        else window.location.assign(url);
      }
    } catch (err) {
      window.location.assign(url);
    }
  }
  // Toasts Django rendered into the page we followed the save to; they would be lost with that page.
  function keptToasts(html) {
    const found = [...html.matchAll(/toasts\.push\(\{ level: '(\w+)', message: '((?:[^'\\]|\\.)*)' \}\)/g)];
    return found.map(([, level, text]) => ({ level, message: text.replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16))).replace(/\\(.)/g, "$1") }));
  }
  async function sideSubmit(e, m) {
    const form = e.target;
    if (form.hasAttribute("hx-post") || form.hasAttribute("hx-get") || form.target === "_blank") return;
    const source = e.submitter?.dataset.confirm ? e.submitter : form;
    if (source.dataset.confirm && form.dataset.confirmed !== "1") return; // the confirm step submits it again
    e.preventDefault();
    // getAttribute, not form.method: a box named "method" (the payment form) hides that property.
    const get = (form.getAttribute("method") || "get").toLowerCase() === "get";
    // A form with no action posts to its own page, which here is the panel's, not the list behind it.
    const action = e.submitter?.getAttribute("formaction") || form.getAttribute("action") || m.url;
    const target = new URL(action, m.url).href;
    const data = new FormData(form, e.submitter || undefined);
    const button = e.submitter;
    if (button) button.disabled = true;
    try {
      const response = get
        ? await fetch(`${target.split("?")[0]}?${new URLSearchParams(data)}`, { credentials: "same-origin" })
        : await fetch(target, { method: "post", body: data, credentials: "same-origin" });
      const html = await response.text();
      if (modal !== m) return;
      if (!response.ok) {
        window.dispatchEvent(new CustomEvent("toast", { detail: { level: "danger", message: "That did not save. Please try again." } }));
        return;
      }
      if (get || !response.redirected) {
        fillSide(m, html, response.url); // shows the form's errors, or the filtered view
        return;
      }
      if (!new DOMParser().parseFromString(html, "text/html").querySelector("#main")) {
        window.location.assign(response.url); // a print page or the sign-in page
        return;
      }
      try {
        sessionStorage.setItem("sd-toasts", JSON.stringify(keptToasts(html)));
      } catch (err) { /* the toast is only a convenience */ }
      m.dirty = false;
      closeModal();
      // A payment lands on the invoice, whose banner holds the receipt to print.
      if (new URL(response.url).searchParams.has("receipt")) window.location.assign(response.url);
      else window.location.reload();
    } catch (err) {
      window.dispatchEvent(new CustomEvent("toast", { detail: { level: "danger", message: "Cannot reach the SehatDesk server. Your typing is still here." } }));
    } finally {
      if (button?.isConnected) button.disabled = false;
    }
  }
  window.addEventListener("load", () => {
    try {
      const kept = JSON.parse(sessionStorage.getItem("sd-toasts") || "[]");
      sessionStorage.removeItem("sd-toasts");
      kept.forEach((t) => window.dispatchEvent(new CustomEvent("toast", { detail: t })));
    } catch (err) { /* nothing kept */ }
  });
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest?.("a[href]");
    if (a) {
      if (!panelWorthy(a) || a.closest(".panel-side [data-tools]")) return;
      e.preventDefault();
      openSide(a, a.closest("tr") ? rowTitle(a.closest("tr")) : a.textContent.trim());
      return;
    }
    // A click on the row itself, not on a control or a text selection, opens the row's main link.
    const tr = e.target.closest?.("tr[data-row-link]");
    if (!tr || e.target.closest("button, input, select, textarea, label, summary, .row-expand") || String(window.getSelection())) return;
    const link = mainLink(tr);
    if (!link) return;
    if (panelWorthy(link)) openSide(link, rowTitle(tr));
    else window.location.assign(link.href);
  });

  // Tables inside a popup get the look but not a second expand column.
  const prepareInPopup = (t) => t.setAttribute("data-plain", "");
  function openRow(tr) {
    if (!tr) return;
    const link = !editable(tr) && mainLink(tr);
    if (link) openLink(tr, link);
    else openFields(tr);
  }

  // ---- Removing a line ----
  function removeButton(tr) {
    return [...tr.querySelectorAll("button, input[type=submit]")].find(
      (b) => shown(b) && (/^(remove|delete)\b/i.test(b.getAttribute("aria-label") || "") || /^(remove|delete)$/i.test(clean(b.textContent || b.value)))
    );
  }
  // The server decides what removal means: deactivate, or cancel with a reason. Nothing is erased here.
  function postTo(url, extra = {}) {
    const token = document.querySelector("[name=csrfmiddlewaretoken]")?.value || JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"];
    const form = document.createElement("form");
    form.method = "post";
    form.action = url;
    Object.entries({ csrfmiddlewaretoken: token, ...extra }).forEach(([name, value]) => {
      form.append(Object.assign(document.createElement("input"), { type: "hidden", name, value }));
    });
    document.body.append(form);
    form.submit();
  }
  async function removeRow(tr) {
    const label = tr.dataset.removeLabel || "Remove";
    if (tr.hasAttribute("data-remove-reason")) {
      const m = openPanel(`${label}: ${rowTitle(tr)}`);
      m.body.innerHTML = '<div class="field"><label class="field-label" for="row-reason">Reason (required)</label><input id="row-reason" class="input" maxlength="200" autocomplete="off"></div><div class="mt-3 flex gap-2"><button type="button" class="btn btn-danger" data-go></button><button type="button" class="btn btn-close" data-close>Close</button></div>';
      const box = m.body.querySelector("input");
      const go = m.body.querySelector("[data-go]");
      go.textContent = label;
      const send = () => {
        if (box.value.trim()) postTo(tr.dataset.remove, { reason: box.value.trim() });
        else box.focus();
      };
      go.addEventListener("click", send);
      box.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
          send();
        }
      });
      box.focus();
      return;
    }
    const ok = await Alpine.store("confirm").ask({ title: `${label}?`, message: tr.dataset.removeNote || rowTitle(tr), actionLabel: label, danger: true });
    if (ok) postTo(tr.dataset.remove);
  }
  async function removeLine(tr, ask) {
    if (tr.dataset.remove) {
      removeRow(tr);
      return;
    }
    const button = removeButton(tr);
    if (!button) {
      window.dispatchEvent(new CustomEvent("toast", { detail: { level: "info", message: "This line cannot be removed here." } }));
      return;
    }
    const confirmed = button.hasAttribute("hx-confirm") || button.dataset.confirm || button.form?.dataset.confirm;
    if (ask && !confirmed) {
      const ok = await Alpine.store("confirm").ask({ title: "Remove this line?", message: rowTitle(tr), actionLabel: "Remove", danger: true });
      if (!ok) return;
    }
    const col = document.activeElement?.closest?.("td")?.cellIndex;
    const next = tr.nextElementSibling || tr.previousElementSibling;
    button.click();
    if (next) setTimeout(() => (next.cells?.[col]?.querySelector(BOXES) || (next.tabIndex === -1 || next.tabIndex === 0 ? next : null))?.focus(), 30);
  }

  // ---- Keys ----
  const typing = (el) => el.matches?.("input, select, textarea, [contenteditable]");
  function moveInCells(e, box, cell) {
    if (e.defaultPrevented || box.getAttribute("aria-expanded") === "true" || box.tagName === "TEXTAREA") return;
    if (/^(date|time|datetime-local|month)$/.test(box.type)) return;
    const row = cell.parentElement;
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      if (box.tagName === "SELECT") return; // up and down choose its option
      e.preventDefault(); // a number box would otherwise count up or down
      // Step over filtered rows, group headings and locked boxes.
      const step = e.key === "ArrowUp" ? "previousElementSibling" : "nextElementSibling";
      for (let other = row[step]; other; other = other[step]) {
        const next = !other.hidden && other.cells[cell.cellIndex]?.querySelector(BOXES);
        if (next && !next.disabled) {
          next.focus();
          return;
        }
      }
      return;
    }
    if (box.tagName === "INPUT" && box.type !== "number" && box.type !== "checkbox") {
      if (box.selectionStart === null) return;
      const atEdge = box.selectionStart === box.selectionEnd && box.selectionStart === (e.key === "ArrowLeft" ? 0 : box.value.length);
      const allSelected = box.value && box.selectionStart === 0 && box.selectionEnd === box.value.length;
      if (!atEdge && !allSelected) return;
    }
    const boxes = [...row.querySelectorAll(BOXES)].filter((b) => shown(b) && !b.disabled);
    e.preventDefault();
    boxes[boxes.indexOf(box) + (e.key === "ArrowLeft" ? -1 : 1)]?.focus();
  }
  function focusRow(tr) {
    if (!tr) return;
    const table = tr.closest("table");
    table.querySelectorAll('tr[tabindex="0"]').forEach((r) => (r.tabIndex = -1));
    tr.tabIndex = 0;
    tr.focus();
  }
  function onRowKey(e, tr) {
    const list = rows(tr.closest("table")).filter(shown);
    const at = list.indexOf(tr);
    const moves = { ArrowDown: list[at + 1], ArrowUp: list[at - 1], Home: list[0], End: list[list.length - 1] };
    if (e.key in moves) {
      e.preventDefault();
      focusRow(moves[e.key]);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const link = mainLink(tr);
      if (link) link.click();
      else openRow(tr);
    } else if (e.key.toLowerCase() === "e" && tr.dataset.edit) { // Caps Lock too
      e.preventDefault();
      openSide({ href: new URL(tr.dataset.edit, window.location.href).href }, rowTitle(tr));
    } else if (e.key === " ") {
      e.preventDefault();
      openRow(tr);
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      removeLine(tr, true);
    }
  }
  const onScreen = (el) => el.getClientRects().length > 0;
  // The open popup on top, if any: a confirm question first, then the newest.
  function topDialog() {
    const open = [...document.querySelectorAll('[role=alertdialog], [role=dialog]')].filter(onScreen);
    return open.find((d) => d.getAttribute("role") === "alertdialog") || open[open.length - 1] || null;
  }
  // getAttribute, not form.method: a box named "method" hides that property.
  const savable = (form) => form?.getAttribute("method")?.toLowerCase() === "post" || form?.hasAttribute("hx-post");
  const hasBoxes = (form) => [...form.querySelectorAll(BOXES)].some((b) => shown(b) && !b.hasAttribute("data-grid-filter"));
  // The form the cursor is in. With the cursor elsewhere, the only form on the screen or popup that has
  // boxes; with several there is no telling which was meant, and a form of buttons alone is an action, not a save.
  function formToSave() {
    const scope = topDialog() || document.getElementById("main");
    if (!scope) return null;
    const form = document.activeElement?.closest?.("form");
    if (savable(form) && scope.contains(form)) return form;
    const forms = [...scope.querySelectorAll("form")].filter((f) => savable(f) && hasBoxes(f));
    return forms.length === 1 ? forms[0] : null;
  }
  function saveForm(e) {
    // Runs after this event: screens with their own F8 have already cancelled it by then.
    setTimeout(() => {
      if (e.defaultPrevented) return;
      const form = formToSave();
      if (!form) return;
      // A form that saves itself on change: leaving the box sends what was typed.
      const trigger = form.hasAttribute("hx-post") && form.getAttribute("hx-trigger");
      if (trigger && !trigger.includes("submit")) {
        const box = document.activeElement;
        box?.blur();
        box?.focus();
        return;
      }
      const button = form.querySelector("button.btn-primary:not([name])");
      if (!button?.disabled) form.requestSubmit(button || undefined);
    });
  }
  // The page's main "make a new one". A link opens it. A button that posts a form with boxes only goes to
  // the first box, and the wider words are for links alone, so this key never creates a record by itself.
  function newOnPage() {
    const target = [...document.querySelectorAll("#main a.btn, #main button.btn")].find((b) => {
      const words = clean(b.textContent);
      return shown(b) && (/^(\+\s*)?(new|add|register|create)\b/i.test(words) || (b.tagName === "A" && /^(\+\s*)?(book|admit|record|receive|start|issue)\b/i.test(words)));
    });
    const firstBox = target?.form && [...target.form.querySelectorAll(BOXES)].find((b) => shown(b) && !b.disabled);
    if (firstBox) firstBox.focus();
    else target?.click();
  }

  document.addEventListener("keydown", (e) => {
    if (sdCombo.handleKey(e)) return;
    const el = e.target;
    const ctrl = e.ctrlKey || e.metaKey;
    const key = e.key.toLowerCase();
    // Tab stays inside the open popup, so Enter can never act on the page behind it.
    const dialog = topDialog();
    if (dialog && key === "tab") {
      const items = [...dialog.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea')].filter((b) => onScreen(b) && b.tabIndex >= 0 && !b.disabled);
      const edge = e.shiftKey ? items[0] : items[items.length - 1];
      if (document.activeElement === edge || !dialog.contains(document.activeElement)) {
        e.preventDefault();
        (e.shiftKey ? items[items.length - 1] : items[0])?.focus();
      }
      return;
    }
    if (dialog?.getAttribute("role") === "alertdialog" && (key === "f8" || (ctrl && key === "s"))) {
      e.preventDefault(); // answer the question first
      return;
    }
    if (modal) {
      if (key === "escape" || (ctrl && key === "e")) {
        e.preventDefault();
        e.stopPropagation();
        askClose();
        return;
      }
      if (key === "enter" && modal.moved.length && el.matches("input, select")) {
        e.preventDefault();
        const boxes = [...modal.body.querySelectorAll(BOXES)].filter((b) => shown(b));
        const next = boxes[boxes.indexOf(el) + 1];
        if (next) next.focus();
        else closeModal();
        return;
      }
      if (key === "f8" && modal.moved.length) closeModal(); // boxes go home, then the form saves
      else if (key !== "f8") return;
    }
    if (ctrl && key === "e") {
      const tr = el.closest?.(`${TABLES.split(", ")[0]} tbody tr, ${TABLES.split(", ")[1]} tbody tr`);
      const target = tr || document.querySelector("#main tr[data-sd-row][tabindex='0']") || document.querySelector("#main tbody tr[data-sd-row]");
      if (target && !isBlank(target)) {
        e.preventDefault();
        openRow(target);
      }
      return;
    }
    if (ctrl && key === "/") {
      e.preventDefault();
      help();
      return;
    }
    if (ctrl && key === "b" && !e.shiftKey) {
      e.preventDefault();
      document.querySelector('[aria-label="Show or hide menu names"]')?.click();
      return;
    }
    if (e.altKey && e.code === "KeyN") {
      e.preventDefault();
      newOnPage();
      return;
    }
    if (key === "f8") {
      saveForm(e);
      return;
    }
    if (ctrl && key === "s") {
      e.preventDefault(); // not the browser's "save page"
      const again = new KeyboardEvent("keydown", { key: "F8", bubbles: true, cancelable: true });
      el.dispatchEvent(again);
      return;
    }
    if (e.altKey && e.code === "KeyV") {
      e.preventDefault();
      const first = () => document.querySelector('#main [name$="bp_systolic"]');
      if (first()?.offsetParent) first().focus();
      else {
        // Reception folds this section away: open it, then go in.
        [...document.querySelectorAll('#main [aria-expanded="false"]')].find((b) => /vitals/i.test(b.textContent))?.click();
        setTimeout(() => first()?.focus(), 30);
      }
      return;
    }
    if (e.altKey && (e.code === "KeyJ" || e.code === "KeyK")) {
      e.preventDefault();
      const firstBox = (card) => [...card.querySelectorAll(BOXES)].find(shown);
      const cards = [...document.querySelectorAll("#main .card")].filter(firstBox);
      const at = cards.indexOf(el.closest?.("#main .card"));
      firstBox(cards[at + (e.code === "KeyJ" ? 1 : -1)] || cards[0])?.focus();
      return;
    }
    if (e.altKey && e.code === "KeyF") {
      e.preventDefault();
      const box = [...document.querySelectorAll('#main input[type=search], #main input[name=q]')].find(shown);
      box?.focus();
      return;
    }
    if (e.altKey && /^Digit[1-9]$/.test(e.code)) {
      e.preventDefault();
      document.querySelectorAll(".sidebar-nav a.nav-link")[Number(e.code.slice(5)) - 1]?.click();
      return;
    }
    const cell = el.closest?.("#main table[data-sd-table] tbody td");
    if (ctrl && (key === "delete" || key === "backspace") && cell && (key === "delete" || e.metaKey)) {
      e.preventDefault();
      removeLine(cell.parentElement, false);
      return;
    }
    if (e.altKey || ctrl) return;
    if (el.matches?.("#main table[data-sd-table] tbody tr")) {
      onRowKey(e, el);
      return;
    }
    if (cell && typing(el) && !e.shiftKey && e.key.startsWith("Arrow")) {
      moveInCells(e, el, cell);
      return;
    }
    // A box in a table inside a form: Enter moves on, unless the screen has its own Enter.
    if (cell && e.key === "Enter" && !e.defaultPrevented && el.matches("input, select") && el.closest("form:is([method=post], [hx-post])")) {
      e.preventDefault();
      sdNextBox(el)?.focus();
      return;
    }
    // Entry forms: Enter goes to the next box and Up/Down to the box before or after, never a surprise
    // save. F8 or Enter on the save button saves. Sign-in forms (data-enter-submits) send on the last box.
    const entry = el.closest?.("form:is([method=post], [hx-post]) .field");
    if (entry && !e.defaultPrevented && !e.shiftKey && el.matches("input, select")) {
      if (e.key === "Enter") {
        e.preventDefault();
        const next = sdNextBox(el);
        if (next?.matches("button") && el.form?.hasAttribute("data-enter-submits")) {
          el.form.requestSubmit();
          return;
        }
        sdNextBox(el)?.focus();
        return;
      }
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && el.tagName === "INPUT" && el.type !== "number" && !/^(date|time|datetime-local|month)$/.test(el.type) && el.getAttribute("aria-expanded") !== "true") {
        e.preventDefault();
        sdNextBox(el, e.key === "ArrowDown" ? 1 : -1)?.focus();
        return;
      }
    }
    // From a search box above a table, Down goes into the table.
    if (e.key === "ArrowDown" && !e.defaultPrevented && el.matches?.('#main input[type=search], #main input[type=text], #main input:not([type])') && el.getAttribute("aria-expanded") !== "true") {
      const first = [...document.querySelectorAll('#main tr[data-sd-row][tabindex="0"]')].find(shown);
      if (first) {
        e.preventDefault();
        first.focus();
      }
      return;
    }
    const emptyFilter = el.matches?.("#main input:not(.field input)") && !el.value && !cell;
    if (typing(el) && !(emptyFilter && "?[]".includes(e.key))) return;
    if (e.key === "[" || e.key === "]") {
      const label = e.key === "[" ? "Prev" : "Next";
      [...document.querySelectorAll('#main nav[aria-label="Pagination"] a')].find((a) => a.textContent.includes(label) || a.getAttribute("aria-label")?.includes(label))?.click();
      return;
    }
    if (e.key === "?") {
      e.preventDefault();
      help();
    } else if (e.key === "ArrowDown" && (el === document.body || el.id === "main")) {
      const first = document.querySelector('#main tr[data-sd-row][tabindex="0"]');
      if (first && shown(first)) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  document.addEventListener("focusin", (e) => {
    if (e.target.matches?.('table[data-sd-table] input:not([type=hidden]):not([type=checkbox]):not([type=radio]), form:is([method=post], [hx-post]) .field input:not([type=checkbox]):not([type=radio]):not([type=file])')) e.target.select();
  });
  document.addEventListener("click", (e) => {
    const remove = e.target.closest?.("[data-row-remove]");
    if (remove) {
      removeRow(remove.closest("tr"));
      return;
    }
    const button = e.target.closest?.("[data-row-open]");
    if (button) {
      openRow(button.closest("tr"));
      return;
    }
    const tr = e.target.closest?.("#main table[data-sd-table] tbody tr");
    if (tr && tr.tabIndex === -1 && !e.target.closest("a, button, input, select, textarea")) focusRow(tr);
  });
  function help() {
    const m = openPanel("Keyboard shortcuts");
    const list = document.createElement("dl");
    list.className = "grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 text-body";
    SHORTCUTS.forEach(([keys, what]) => {
      const dt = document.createElement("dt");
      dt.innerHTML = "<kbd class=\"kbd\"></kbd>";
      dt.firstChild.textContent = keys;
      const dd = document.createElement("dd");
      dd.textContent = what;
      list.append(dt, dd);
    });
    m.body.append(list);
    m.body.focus();
  }

  document.addEventListener("DOMContentLoaded", () => {
    setTimeout(() => {
      scan(document.body);
      new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach(scan))).observe(document.body, { childList: true, subtree: true });
    }, 0);
  });
  return { help };
})();

// ---- First box gets the cursor --------------------------------------------------------
// Every screen, and every popup, starts with the cursor in its first box, or the first with an error
// after a failed save. It never scrolls the page to reach one, a dropdown stays closed until asked,
// and a screen that already placed the cursor keeps it. Phones are left alone (no surprise keyboard).
// With no box on the first view, the page itself takes the cursor: Tab starts at its content, not the menu.
const sdFocusFirst = (root) => {
  if (!window.matchMedia("(pointer: fine)").matches) return;
  const BOXES = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]):not([readonly]):not([data-grid-filter]), textarea";
  const boxes = [...root.querySelectorAll(BOXES)].filter((b) => b.offsetParent !== null && !b.disabled && b.tabIndex >= 0 && !b.closest("details:not([open])"));
  const bad = boxes.find((b) => b.getAttribute("aria-invalid") === "true" || b.closest(".field")?.querySelector(".field-error"));
  let box = bad || boxes[0];
  if (!bad && !(box?.getBoundingClientRect().top < window.innerHeight)) {
    if (root.id !== "main") return;
    root.tabIndex = -1;
    box = root;
  }
  window.sdQuietFocus = true;
  box.focus({ preventScroll: !bad });
  window.sdQuietFocus = false;
};
document.addEventListener("DOMContentLoaded", () => {
  // After the searchable dropdowns are built and Alpine has run its own x-init focus.
  setTimeout(() => {
    const main = document.getElementById("main");
    if (main && document.activeElement === document.body) sdFocusFirst(main);
  }, 60);
});
document.addEventListener("htmx:afterSettle", (e) => {
  const root = e.target.closest?.("#overlay-root");
  if (root) setTimeout(() => sdFocusFirst(root), 60);
});

// ---- A long form keeps its save button in view ------------------------------------------
// Only when the form starts on screen and its button is below the window. action-bar does this by hand.
const sdStickySave = (root = document.getElementById("main")) => {
  // A screen with its own always-visible action bar needs no second bar: the two would cover each other.
  if (!root || root.querySelector("[data-page-actions]")) return;
  root.querySelectorAll("form").forEach((form) => {
    const saves = [...form.querySelectorAll("button.btn-primary")].filter((b) => b.offsetParent !== null);
    const save = saves[saves.length - 1];
    if (!save || save.closest(".action-bar, .save-bar, table")) return;
    if (form.getBoundingClientRect().top > window.innerHeight || save.getBoundingClientRect().bottom <= window.innerHeight) return;
    // The widest short block around the button, such as its row; a button alone in the form gets its own.
    let bar = save;
    while (bar.parentElement !== form && bar.parentElement.offsetHeight < 120) bar = bar.parentElement;
    if (bar === save) {
      bar = document.createElement("div");
      save.before(bar);
      bar.append(save);
    }
    bar.classList.add("save-bar");
  });
};
document.addEventListener("DOMContentLoaded", () => setTimeout(() => sdStickySave(), 80));
document.addEventListener("htmx:afterSettle", () => setTimeout(() => sdStickySave(), 80));

// ---- A value outside its reference range says so as you type ----------------------------
document.addEventListener("input", (e) => {
  const box = e.target;
  if (!box.matches?.("[data-ref-low], [data-ref-high]")) return;
  const value = parseFloat(box.value);
  const { refLow, refHigh } = box.dataset;
  let word = "";
  if (!Number.isNaN(value) && refLow !== "" && value < parseFloat(refLow)) word = "Low";
  if (!Number.isNaN(value) && refHigh !== "" && value > parseFloat(refHigh)) word = "High";
  const field = box.closest(".field");
  let tag = field?.querySelector(".ref-state");
  if (field && !tag) {
    tag = document.createElement("span");
    tag.className = "ref-state";
    field.querySelector(".field-label")?.append(tag);
  }
  if (tag) tag.textContent = word;
});

// ---- Hints: anything with a title explains itself at once on hover or keyboard focus ----
(() => {
  const tip = document.createElement("div");
  tip.className = "sd-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  let owner = null;
  const hide = () => {
    if (owner) owner.setAttribute("title", owner.dataset.sdTitle);
    owner = null;
    tip.hidden = true;
  };
  const show = (el) => {
    hide();
    const text = el.getAttribute("title");
    if (!text) return;
    owner = el;
    el.dataset.sdTitle = text;
    el.removeAttribute("title"); // or the browser shows its own slow copy as well
    if (!el.hasAttribute("aria-label") && !el.textContent.trim()) el.setAttribute("aria-label", text);
    tip.textContent = text;
    if (!tip.isConnected) document.body.append(tip);
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    tip.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - tip.offsetWidth - 8))}px`;
    const below = r.bottom + tip.offsetHeight + 8 < window.innerHeight;
    tip.style.top = `${below ? r.bottom + 6 : r.top - tip.offsetHeight - 6}px`;
  };
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest?.("[title]");
    if (el && el !== owner) show(el);
    else if (owner && !owner.contains(e.target)) hide();
  });
  document.addEventListener("focusin", (e) => {
    if (e.target.matches?.("[title]")) show(e.target);
  });
  ["focusout", "scroll", "click", "keydown"].forEach((name) => document.addEventListener(name, hide, true));
})();

// ---- Back and breadcrumbs: every page says where you came from and what is above it ----------
// Each browser tab keeps its own trail. Coming back to a page returns to its filters and scroll position.
// A form page that has been saved leaves the trail, so Back never returns to a form already sent.
(() => {
  const box = document.getElementById("page-trail");
  const KEY = "sd-trail";
  const FORM_PAGE = /\/(new|edit|create|book|register|visit|walk-in|admit|pay|results)\/?$/;
  const read = (key, fallback) => {
    try {
      return JSON.parse(sessionStorage.getItem(key)) ?? fallback;
    } catch (e) {
      return fallback;
    }
  };
  const write = (key, value) => {
    try {
      sessionStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* no trail: Back falls back to the page above */ }
  };
  if (!box) {
    if (location.pathname.includes("/login")) write(KEY, []);
    return;
  }
  const path = location.pathname;
  const title = (document.querySelector("h1.page-title")?.textContent || document.title.split("·")[0]).trim();
  let trail = read(KEY, []);
  const posted = read("sd-trail-posted", false);
  const cameBack = read("sd-trail-back", false);
  write("sd-trail-posted", false);
  write("sd-trail-back", false);
  const top = trail[trail.length - 1];
  if (posted && top && top.path !== path && FORM_PAGE.test(top.path)) trail.pop();
  const seen = trail.findIndex((t) => t.path === path);
  const y = seen >= 0 ? trail[seen].y : 0;
  if (seen >= 0) trail = trail.slice(0, seen);
  trail.push({ path, url: path + location.search, title, y: 0 });
  trail = trail.slice(-20);
  write(KEY, trail);
  if (cameBack && y) window.addEventListener("load", () => window.scrollTo(0, y));
  window.addEventListener("pagehide", () => {
    const now = read(KEY, []);
    if (now.length) now[now.length - 1].y = window.scrollY;
    write(KEY, now);
  });
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if ((form.getAttribute("method") || "").toLowerCase() === "post" && !form.hasAttribute("hx-post")) write("sd-trail-posted", true);
  }, true);

  const crumbLink = (label, url) => Object.assign(document.createElement("a"), { href: url, textContent: label, className: "hover:text-teal-deep hover:underline" });
  const chevron = '<svg xmlns="http://www.w3.org/2000/svg" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>';
  const addCrumb = (ol, node, last) => {
    const li = document.createElement("li");
    li.className = "flex items-center gap-1";
    li.append(node);
    if (!last) li.insertAdjacentHTML("beforeend", chevron);
    return li;
  };
  const home = document.querySelector(".sidebar-brand a[href]");
  const atHome = home && home.pathname === path;
  let nav = box.querySelector('nav[aria-label="Breadcrumb"]');
  if (!nav && !atHome) {
    nav = document.createElement("nav");
    nav.setAttribute("aria-label", "Breadcrumb");
    nav.innerHTML = '<ol class="flex flex-wrap items-center gap-1 text-label text-muted"></ol>';
    const ol = nav.firstChild;
    const section = document.querySelector(".sidebar a[aria-current=page]");
    if (section && section.pathname !== path) {
      const label = section.getAttribute("aria-label") || section.textContent.trim();
      ol.append(addCrumb(ol, crumbLink(label, section.href)));
    }
    const here = Object.assign(document.createElement("span"), { textContent: title, className: "text-ink" });
    here.setAttribute("aria-current", "page");
    ol.append(addCrumb(ol, here, true));
    box.append(nav);
  }
  const ol = nav?.querySelector("ol");
  if (ol && home && !atHome) ol.prepend(addCrumb(ol, crumbLink("Home", home.href)));

  // The page above: the last crumb with a link.
  const crumbs = ol ? [...ol.querySelectorAll("a[href]")] : [];
  const parent = crumbs[crumbs.length - 1];
  const previous = trail[trail.length - 2];
  const target = previous ? previous.url : parent?.getAttribute("href");
  const name = previous ? previous.title : parent?.textContent.trim();
  if (target && name) {
    const back = document.createElement("a");
    back.href = target;
    back.className = "btn btn-secondary btn-sm max-w-xs";
    back.dataset.panel = "off";
    back.title = "Back (Alt + ←)";
    back.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 19-7-7 7-7"/><path d="M19 12H5"/></svg><span class="truncate"></span>';
    back.lastChild.textContent = `Back to ${name}`;
    back.addEventListener("click", () => write("sd-trail-back", true));
    box.prepend(back);
  }
  document.addEventListener("keydown", (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey || !parent) return;
    if (e.key === "ArrowUp") {
      e.preventDefault();
      parent.click();
    }
  });
})();

// ---- Drafts: what is typed in a form is kept for its owner, so a stray Back or closed tab loses nothing ----
// Any form that posts, with at least two boxes, on a page or in the side panel. data-draft="off" opts out; forms
// sent by htmx are left alone. Nothing fills in by itself: the form offers Restore, and only saving makes a record.
const sdDrafts = (() => {
  const BASE = "/drafts/";
  const SKIP_TYPES = new Set(["hidden", "password", "file", "submit", "button", "reset", "image"]);
  const fields = (form) => [...form.elements].filter((el) => el.name && !el.disabled && !SKIP_TYPES.has(el.type) && el.tagName !== "BUTTON" && el.tagName !== "FIELDSET");
  const csrf = (form) => form.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
  // Rows that exist only in Alpine (purchase items, stock grid): saved as the list they are drawn from.
  const rowHolders = (form) => (window.Alpine ? [...form.querySelectorAll("[x-data]")].filter((el) => {
    const d = window.Alpine.$data(el);
    return Array.isArray(d.rows) || Array.isArray(d.lines);
  }) : []);
  const listOf = (d) => (Array.isArray(d.rows) ? "rows" : "lines");

  function read(form) {
    const values = {};
    fields(form).forEach((el) => {
      const value = el.type === "checkbox" || el.type === "radio" ? el.checked : el.value;
      (values[el.name] ||= []).push(value);
    });
    const rows = rowHolders(form).map((el) => {
      const d = window.Alpine.$data(el);
      return JSON.parse(JSON.stringify(d[listOf(d)]));
    });
    return { values, rows };
  }
  async function write(form, data) {
    rowHolders(form).forEach((el, i) => {
      const d = window.Alpine.$data(el);
      if (data.rows?.[i]) {
        const list = listOf(d);
        d[list] = data.rows[i];
        if ("uid" in d) d.uid = data.rows[i].reduce((top, r) => Math.max(top, r.uid || 0), 0);
      }
    });
    await new Promise((resolve) => window.Alpine ? window.Alpine.nextTick(resolve) : resolve());
    const seen = {};
    fields(form).forEach((el) => {
      const i = (seen[el.name] = (seen[el.name] ?? -1) + 1);
      const value = data.values?.[el.name]?.[i];
      if (value === undefined) return;
      if (el.type === "checkbox" || el.type === "radio") el.checked = value;
      else el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  function post(url, body, beacon) {
    const data = new URLSearchParams(body);
    if (beacon) return navigator.sendBeacon(url, data);
    return fetch(url, { method: "post", body: data, credentials: "same-origin" }).catch(() => null);
  }
  function note(form, parts) {
    let box = form.querySelector(":scope > [data-draft-note]");
    if (!box) {
      box = document.createElement("div");
      box.dataset.draftNote = "";
      // col-span-full: many forms are themselves grids; the note takes a whole row, not a cell.
      box.className = "col-span-full mb-3 flex flex-wrap items-center gap-2 rounded-control bg-page p-2 text-label";
      form.prepend(box);
    }
    box.replaceChildren(...parts);
    return box;
  }
  const button = (text, css, onClick) => {
    const b = Object.assign(document.createElement("button"), { type: "button", textContent: text, className: `btn btn-sm ${css}` });
    b.addEventListener("click", onClick);
    return b;
  };
  const words = (text) => Object.assign(document.createElement("span"), { textContent: text });

  function attachForm(form, index, path, saved) {
    if (form.dataset.draftReady) return;
    form.dataset.draftReady = "1";
    const key = form.getAttribute("action") ? `${index}:${new URL(form.getAttribute("action"), location.href).pathname}` : String(index);
    const base = () => ({ path, form: key, csrfmiddlewaretoken: csrf(form) });
    let timer = null;
    let pending = false;
    const body = () => ({ ...base(), title: document.querySelector(".panel-side h2")?.textContent.trim() || document.title.split("·")[0].trim(), data: JSON.stringify(read(form)) });
    const flush = async () => {
      clearTimeout(timer);
      if (!pending) return;
      pending = false;
      const response = await post(`${BASE}save/`, body());
      if (response?.ok && !form.querySelector("[data-draft-offer]")) {
        const at = (await response.json()).saved;
        note(form, [words(`Draft saved ${at}. Safe to leave and come back.`)]);
      }
    };
    const discard = () => {
      pending = false;
      clearTimeout(timer);
      return post(`${BASE}discard/`, base());
    };
    form.addEventListener("input", (e) => {
      if (!e.isTrusted) return; // Restore and Alpine set boxes too; only real typing counts
      pending = true;
      clearTimeout(timer);
      timer = setTimeout(flush, 1500);
    });
    form.addEventListener("change", (e) => {
      if (!e.isTrusted) return;
      pending = true;
      clearTimeout(timer);
      timer = setTimeout(flush, 1500);
    });
    // Rows added with a button or a scan are not typing into this form's boxes.
    form.addEventListener("click", () => { if (rowHolders(form).length) { pending = true; clearTimeout(timer); timer = setTimeout(flush, 1500); } });
    window.addEventListener("pagehide", () => {
      if (pending) post(`${BASE}save/`, body(), true);
    });
    form.addEventListener("submit", () => {
      pending = false;
      clearTimeout(timer);
      post(`${BASE}discard/`, base(), true);
    });
    if (saved) {
      const offer = note(form, [
        words(`You have an unsaved draft from ${saved.saved}.`),
        button("Restore", "btn-primary", async () => {
          await write(form, saved.data);
          offer.remove();
          delete offer.dataset.draftOffer;
          sdFocusFirst(form);
        }),
        button("Discard", "btn-close", () => { discard(); offer.remove(); }),
      ]);
      offer.dataset.draftOffer = "";
    }
  }
  async function attach(root, path) {
    if (!root) return;
    const forms = [...root.querySelectorAll("form")].filter((f) =>
      (f.getAttribute("method") || "").toLowerCase() === "post" && f.dataset.draft !== "off" &&
      !f.hasAttribute("hx-post") && fields(f).length >= 2);
    if (!forms.length) return;
    let saved = [];
    try {
      const r = await fetch(`${BASE}for-page/?path=${encodeURIComponent(path)}`, { credentials: "same-origin" });
      if (r.ok) saved = (await r.json()).drafts;
    } catch (err) { /* drafts are a convenience */ }
    forms.forEach((form, i) => {
      const action = form.getAttribute("action");
      const key = action ? `${i}:${new URL(action, location.href).pathname}` : String(i);
      attachForm(form, i, path, saved.find((d) => d.form === key));
    });
  }
  document.addEventListener("DOMContentLoaded", () => setTimeout(() => attach(document.getElementById("main"), location.pathname + location.search), 120));
  return { attach };
})();

// ---- Entry grids fit the window: rows scroll inside the table, totals and save buttons stay on screen ----
const sdFitGrids = (() => {
  const MIN = 240;
  const grids = () => [...document.querySelectorAll("#main .table-wrap, #main .sheet-wrap")].filter((w) => w.querySelector(":scope .entry-row, :scope table.sheet"));
  function fit() {
    const main = document.getElementById("main");
    const list = grids();
    if (!main || !list.length || window.innerWidth < 768) {
      list.forEach((w) => w.classList.remove("grid-fits"));
      return;
    }
    const scrollY = window.scrollY;
    list.forEach((wrap) => {
      const inner = wrap.scrollTop;
      wrap.classList.add("grid-fits");
      wrap.style.setProperty("--fit", "0px");
      // With the grid at nothing, what the rest of the page needs is what the grid cannot have.
      const bottom = Math.max(...[...main.children].map((c) => c.getBoundingClientRect().bottom));
      const pad = parseFloat(getComputedStyle(main).paddingBottom) || 0;
      const footer = main.parentElement.getBoundingClientRect().bottom - main.getBoundingClientRect().bottom;
      let room = window.innerHeight - (bottom + pad + footer + window.scrollY);
      // A card stretched by its neighbour (the till's summary) has free room inside it too.
      const card = wrap.closest(".card");
      if (card?.lastElementChild) {
        const padCard = parseFloat(getComputedStyle(card).paddingBottom) || 0;
        room += Math.max(0, card.getBoundingClientRect().bottom - padCard - card.lastElementChild.getBoundingClientRect().bottom);
      }
      wrap.style.setProperty("--fit", `${Math.max(MIN, Math.floor(room))}px`);
      wrap.scrollTop = inner;
    });
    window.scrollTo(0, scrollY);
    place();
  }
  function place() {
    document.querySelectorAll(".grid-fits .entry-row .absolute").forEach((el) => {
      const box = el.parentElement.getBoundingClientRect();
      const top = box.bottom + 4;
      Object.assign(el.style, { left: `${box.left}px`, top: `${top}px`, width: `${box.width}px`, maxHeight: `${Math.max(120, window.innerHeight - top - 8)}px` });
    });
  }
  const later = () => setTimeout(fit, 90);
  document.addEventListener("DOMContentLoaded", later);
  document.addEventListener("htmx:afterSettle", later);
  window.addEventListener("resize", later);
  document.addEventListener("scroll", place, { capture: true, passive: true });
  document.addEventListener("focusin", place);
  document.addEventListener("input", place);
  return { fit };
})();

// Scan boxes: <input data-scan>. A scanner types fast and may send a group separator as Ctrl+] (which a
// browser drops) or a line break inside a QR code (which looks like Enter). Keep both, so one code stays one
// value. The server reads it (apps/pharmacy/scan.py). data-scan="fill" swaps a QR code for its product code;
// data-scan="detect" is a name box that hands a scanned code to the barcode box beside it.
const sdScan = (() => {
  const GS = "\x1d";
  const SCANNER_GAP = 40; // ms between keys; nobody types this fast
  const SETTLE = 80; // ms to wait after a fast Enter, in case the code carries on
  let lastKey = 0, fast = 0, held = null;
  const LOOKS_CODED = /^\d{8,14}$|^\][A-Za-z]\d|^\(0[12]\)|^0[12]\d{14}|^https?:\/\/|\x1d|(gtin|ean|batch|lot|exp)[^:=]{0,12}[:=]/i;

  function insert(box, text) {
    box.setRangeText(text, box.value.length, box.value.length, "end");
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function toast(level, message) {
    window.dispatchEvent(new CustomEvent("toast", { detail: { level, message } }));
  }
  function setBox(box, value) {
    box.value = value;
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }
  async function read(box) {
    const code = box.value.trim();
    if (code.length < 4 || box.dataset.scanRead === code) return;
    let data;
    try {
      data = await fetch(`${box.dataset.scanUrl}?code=${encodeURIComponent(code)}`).then((r) => r.json());
    } catch {
      return; // the form still works: the server reads the code again on save
    }
    const scan = data.scan, around = box.closest("tr") || box.form;
    const barcode = box.name === "barcode" ? box : around?.querySelector("[name=barcode]");
    if (box.dataset.scan === "detect") setBox(box, "");
    if (barcode && scan.code) {
      setBox(barcode, scan.code);
      barcode.dataset.scanRead = scan.code;
    }
    const other = data.products.find((p) => String(p.id) !== box.dataset.scanSelf);
    if (other) toast("warning", `This code is already on ${other.name}.`);
    else if (scan.summary) toast("info", `Read ${scan.summary}.${scan.batch ? " Batch and expiry are entered when stock is received." : ""}`);
  }
  function enter(box) {
    if (box.dataset.scan === "detect" && LOOKS_CODED.test(box.value.trim())) {
      read(box).then(() => box.focus());
      return true;
    }
    if (box.dataset.scan === "fill") read(box);
    return false;
  }
  function finish(box) {
    held = null;
    if (enter(box)) return;
    const again = new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, cancelable: true });
    // Enter sends a form only when a person presses it, so do what the browser would have done.
    if (box.dispatchEvent(again)) box.form?.requestSubmit();
  }
  document.addEventListener("keydown", (e) => {
    const box = e.target;
    if (!e.isTrusted || !box.matches?.("input[data-scan]")) return;
    const now = performance.now();
    fast = now - lastKey < SCANNER_GAP ? fast + 1 : 0;
    lastKey = now;
    const separator = e.key === "GroupSeparator" || e.key === GS || (e.ctrlKey && !e.altKey && e.code === "BracketRight");
    if (separator) {
      e.preventDefault();
      e.stopImmediatePropagation();
      insert(box, GS);
      return;
    }
    if (e.key === "Enter") {
      if (fast >= 4) {
        e.preventDefault();
        e.stopImmediatePropagation();
        clearTimeout(held);
        held = setTimeout(() => finish(box), SETTLE);
      } else if (enter(box)) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
      return;
    }
    if (held && e.key.length === 1) {
      clearTimeout(held); // more of the same code: that Enter was a line break inside it
      held = null;
      insert(box, GS);
    }
  }, true);
  document.addEventListener("change", (e) => {
    if (e.target.matches?.("input[data-scan='fill']")) read(e.target);
  }, true);
  return { read };
})();
