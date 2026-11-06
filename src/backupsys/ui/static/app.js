// backup-sys: piccoli comportamenti dell'interfaccia (nessuno script inline: CSP 'self').
(function () {
  "use strict";

  // Conferma per le azioni importanti: <button data-confirm="...">
  document.addEventListener("click", function (ev) {
    const el = ev.target.closest("[data-confirm]");
    if (el && !window.confirm(el.getAttribute("data-confirm"))) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);

  // <select data-autosubmit> invia il form al cambio
  document.querySelectorAll("select[data-autosubmit]").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });

  // Campi della programmazione visibili in base alla frequenza scelta
  const kind = document.getElementById("schedule_kind");
  if (kind) {
    const sync = function () {
      document.querySelectorAll("[data-show]").forEach(function (el) {
        el.classList.toggle("hidden", el.getAttribute("data-show").split(" ").indexOf(kind.value) < 0);
      });
    };
    kind.addEventListener("change", sync);
    sync();
  }

  // Avanzamento in tempo reale sulla panoramica
  const live = document.getElementById("live");
  if (live) {
    const PHASES = { scan: "Analisi dei file…", plan: "Confronto con il backup…", copy: "Copia in corso", done: "Completato" };
    let wasRunning = !live.classList.contains("hidden");
    const tick = function () {
      fetch("/api/status", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s) return;
          const r = s.running;
          if (!r) {
            if (wasRunning) { window.location.reload(); }
            live.classList.add("hidden");
            return;
          }
          wasRunning = true;
          live.classList.remove("hidden");
          document.getElementById("live-title").textContent = r.label || ("Backup «" + r.job_name + "»" + (r.dest ? " → " + r.dest : ""));
          document.getElementById("live-phase").textContent = r.kind === "verify" ? "Verifica in corso" : (PHASES[r.phase] || r.phase);
          const bar = document.getElementById("live-bar");
          if (r.phase === "copy" && r.bytes_total > 0) {
            bar.value = Math.round(100 * r.bytes_done / r.bytes_total);
          } else if (r.phase === "copy" && r.files_total > 0) {
            bar.value = Math.round(100 * r.files_done / r.files_total);
          } else {
            bar.removeAttribute("value");
          }
          document.getElementById("live-detail").textContent = r.phase === "copy"
            ? r.files_done + " / " + r.files_total + " file · " + r.bytes_done_h + " / " + r.bytes_total_h + " · " + r.current
            : "";
        })
        .catch(function () {});
    };
    tick();
    setInterval(tick, 1500);
  }
})();
