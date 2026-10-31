// Print goes through the server (apps/printing): to this PC's print helper, or for 80 mm slips to the
// token printer the server prints on itself. With neither, the browser's own print window opens.
// A page with <meta name="sd-print-dialog"> always opens the print window, so staff pick the printer.
(function () {
  const nativePrint = window.print.bind(window);
  const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content || "";
  const WAIT_MS = 35000;
  const MAX_IMAGE_PX = 600;
  let busy = false;

  function paper() {
    const declared = meta("sd-paper");
    if (declared) return declared;
    let size = "";
    for (const sheet of document.styleSheets) {
      let rules = [];
      try { rules = sheet.cssRules; } catch { continue; }
      for (const rule of rules) {
        if (rule.type === CSSRule.PAGE_RULE && rule.style.getPropertyValue("size")) {
          size = rule.style.getPropertyValue("size");
        }
      }
    }
    if (/^a5/i.test(size)) return "a5";
    const mm = /^([\d.]+)mm/.exec(size);
    if (mm) return parseFloat(mm[1]) <= 60 ? "label" : parseFloat(mm[1]) <= 90 ? "thermal" : "a4";
    return "a4";
  }

  function dataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  // A large logo must not push the page past the server's size limit: paper needs no more than this.
  async function smallImage(blob) {
    try {
      const bitmap = await createImageBitmap(blob);
      const scale = Math.min(1, MAX_IMAGE_PX / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return canvas.toDataURL("image/png");
    } catch { return dataUrl(blob); }
  }

  // A complete page that needs nothing from the network: the helper's renderer has none.
  async function snapshot() {
    const page = document.documentElement.cloneNode(true);
    page.querySelectorAll("script, .no-print, [hidden], meta[name^='sd-print']").forEach((el) => el.remove());
    for (const link of page.querySelectorAll("link[rel='stylesheet']")) {
      const css = await fetch(link.href).then((r) => r.text());
      const style = document.createElement("style");
      style.textContent = css;
      link.replaceWith(style);
    }
    for (const img of page.querySelectorAll("img[src]")) {
      if (img.src.startsWith("data:")) continue;
      img.src = await fetch(img.src).then((r) => r.blob()).then(smallImage);
    }
    return "<!doctype html>" + page.outerHTML;
  }

  function notice(text) {
    let box = document.getElementById("print-notice");
    if (!box) {
      box = document.createElement("div");
      box.id = "print-notice";
      box.className = "no-print";
      box.setAttribute("role", "status");
      box.style.cssText = "position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:50;" +
        "padding:8px 16px;border-radius:8px;background:#112d4e;color:#fff;font-size:14px";
      document.body.appendChild(box);
    }
    box.textContent = text;
    return box;
  }

  async function waitForPrinter(id, base) {
    const started = Date.now();
    while (Date.now() - started < WAIT_MS) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const job = await fetch(`${base}${id}/`).then((r) => r.json());
      if (job.status !== "pending" && job.status !== "sending") return job;
    }
    return { status: "timeout" };
  }

  window.print = async function () {
    if (meta("sd-print-dialog")) { nativePrint(); return; }
    if (busy) return;
    busy = true;
    const base = meta("sd-print-jobs");
    let sent = null;
    try {
      const response = await fetch(base, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": meta("sd-print-csrf") },
        body: JSON.stringify({ paper: paper(), html: await snapshot() }),
      });
      if (response.ok) sent = await response.json();
    } catch { sent = null; }
    if (sent && sent.direct) {
      busy = false;
      if (sent.error) {
        notice(sent.error);
        nativePrint();
        return;
      }
      const box = notice(`Printed on ${sent.printer}.`);
      setTimeout(() => box.remove(), 3000);
      window.dispatchEvent(new Event("afterprint"));
      return;
    }
    if (!sent || !sent.queued) {
      busy = false;
      nativePrint();
      return;
    }
    const box = notice(`Printing on ${sent.printer}...`);
    let job;
    try { job = await waitForPrinter(sent.id, base); } catch { job = { status: "timeout" }; }
    busy = false;
    if (job.status === "done") {
      box.textContent = `Printed on ${sent.printer}.`;
      setTimeout(() => box.remove(), 3000);
      window.dispatchEvent(new Event("afterprint"));
    } else if (job.status === "sending" || job.status === "timeout") {
      // It may still come out: opening the window here could print it twice.
      box.textContent = `${sent.printer} has not answered. Check the printer before printing again.`;
    } else {
      box.remove();
      nativePrint();
    }
  };
})();
