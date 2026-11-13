"use strict";

const ProjectVerification = (() => {
  const labels = {
    title: ["Verification report", "تقرير التحقق"],
    build: ["Build", "البناء"], test: ["Tests", "الاختبارات"],
    passed: ["Passed", "نجح"], failed: ["Failed", "فشل"], no_tests: ["No tests found", "لم تُكتشف اختبارات"],
    skipped: ["Skipped", "تُخطّي"], unavailable: ["Unavailable", "غير متاح"],
    timed_out: ["Timed out", "انتهت المهلة"], setup_failed: ["Setup failed", "تعذّر الإعداد"],
    unsupported: ["No checks detected", "لم تُكتشف فحوص"], incomplete: ["Incomplete", "غير مكتمل"],
    not_run: ["Not checked", "لم يُفحص"],
    running: ["Running checks…", "الفحص جارٍ…"], stale: ["Files changed. Run checks again.", "تغيّرت الملفات. أعد تشغيل الفحص."],
    current: ["Matches the checked snapshot", "مطابق للنسخة المفحوصة"],
    unknown: ["Could not verify freshness. Reconnect the folder or rerun checks.", "تعذّر التحقق من حداثة النتائج. أعد ربط المجلد أو شغّل الفحص مجددًا."],
    pending: ["Checking for file changes…", "جارٍ التحقق من تغيّر الملفات…"],
    inactive: ["Select this project to verify freshness.", "اختر هذا المشروع للتحقق من حداثة النتائج."],
    count: ["Tests run", "الاختبارات المنفّذة"], partial: ["known, some runners did not report a count", "معلومة، وبعض الأدوات لم تعرض العدد"],
    no_count: ["Count unavailable", "العدد غير متاح"], first: ["First error", "أول خطأ"],
    untested: ["What wasn’t checked", "ما لم يُفحص"],
    links: ["Linked files and folders were excluded.", "استُبعدت الملفات والمجلدات المرتبطة."],
    dependency: ["Missing dependency", "تبعية مفقودة"],
    command: ["Command", "الأمر"],
  };
  const words = (key, ar) => (labels[key] || labels.incomplete)[ar ? 1 : 0];
  let current = null, inFlight = false, generation = 0;

  function status(rows) {
    if (!rows.length) return "not_run";
    if (rows.some(row => row.status === "failed")) return "failed";
    if (rows.every(row => row.status === rows[0].status)) return rows[0].status;
    return "incomplete";
  }

  function freshness(revision, project, folder) {
    if (!folder || project !== folder.path) return "inactive";
    if (current?.token !== folder.token) return "pending";
    if (current.state !== "current") return current.state;
    return current.revision === revision ? "current" : "stale";
  }

  function paint() {
    const folder = window.projectFolderForChat?.();
    for (const card of document.querySelectorAll("[data-verification-revision]")) {
      const state = freshness(card.dataset.verificationRevision, card.dataset.verificationProject, folder);
      const line = card.querySelector(".verification-freshness");
      if (line.dataset.state !== state) {
        line.dataset.state = state;
        line.textContent = words(state, card.lang === "ar");
      }
    }
  }

  async function refresh(force = false) {
    const folder = window.projectFolderForChat?.();
    if (!folder || inFlight || !document.querySelector("[data-verification-revision]")) {
      if (force) paint();
      return;
    }
    const id = generation;
    inFlight = true;
    try {
      const response = await fetch("project/revision", {
        method: "POST", headers: headers(true), body: JSON.stringify({token: folder.token}),
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error();
      const {revision} = await response.json();
      if (generation === id) current = {token: folder.token, state: "current", revision};
    } catch {
      if (generation === id && (current?.token !== folder.token || current.state !== "stale")) {
        current = {token: folder.token, state: "unknown"};
      }
    } finally {
      inFlight = false;
      paint();
    }
  }

  function changed() {
    generation++;
    const folder = window.projectFolderForChat?.();
    if (folder) current = {token: folder.token, state: "stale"};
    paint();
  }

  function card(report, project, ar) {
    const label = key => words(key, ar);
    const rows = report.checks || [];
    const build = rows.filter(row => row.kind === "build");
    const tests = rows.filter(row => row.kind === "test");
    const counted = tests.filter(row => Number.isInteger(row.test_count));
    const sum = counted.reduce((n, row) => n + row.test_count, 0);
    const missing = rows.filter(row => !["passed", "failed"].includes(row.status));
    const blocked = rows.filter(row => row.missing_dependency);
    const error = rows.find(row => row.first_error)?.first_error;
    const revision = report.revision;
    const state = revision ? freshness(revision, project, window.projectFolderForChat?.()) : null;
    const count = counted.length ? `${fmt(sum)}${counted.length < tests.length ? ` · ${label("partial")}` : ""}`
      : tests.some(row => ["passed", "failed", "timed_out"].includes(row.status)) ? label("no_count") : fmt(0);
    const metric = (key, value) => `<div><dt>${label(key)}</dt><dd>${esc(value)}</dd></div>`;
    return `<section class="verification-card" aria-label="${label("title")}" lang="${ar ? "ar" : "en"}" dir="${ar ? "rtl" : "ltr"}"
      ${revision ? `data-verification-revision="${esc(revision)}" data-verification-project="${esc(project)}"` : ""}>
      <header>${icon("check", "st-icon st-icon--sm")} <strong>${label("title")}</strong><span>${label(report.status)}</span></header>
      ${report.status === "running" ? "" : `<dl>${metric("build", label(status(build)))}${metric("test", label(status(tests)))}${metric("count", count)}</dl>`}
      ${report.message ? `<p dir="auto">${esc(report.message)}</p>` : ""}
      ${error ? `<div class="verification-error"><strong>${label("first")}</strong>
        ${error.file ? `<bdi><code dir="ltr">${esc(error.file)}:${esc(error.line)}</code></bdi>` : ""}
        <p dir="auto">${esc(error.message)}</p></div>` : ""}
      ${blocked.length ? `<ul class="verification-dependencies">${blocked.map(row =>
        `<li><strong>${label("dependency")}</strong> <bdi>${esc(row.missing_dependency)}</bdi>
          <div>${label("command")} <bdi><code dir="ltr">${esc(row.command || row.check)}</code></bdi>
          ${row.cwd && row.cwd !== "." ? `<bdi>(${esc(row.cwd)})</bdi>` : ""}</div></li>`).join("")}</ul>` : ""}
      ${report.status !== "running" && (!build.length || !tests.length || missing.length || report.skipped_links)
        ? `<div><strong>${label("untested")}</strong><ul>
          ${!build.length ? `<li>${label("build")} · ${label("not_run")}</li>` : ""}
          ${!tests.length ? `<li>${label("test")} · ${label("not_run")}</li>` : ""}
          ${missing.map(row => `<li><bdi>${esc(row.command || row.check)}</bdi> · ${label(row.status)}</li>`).join("")}
          ${report.skipped_links ? `<li>${label("links")}</li>` : ""}
        </ul></div>` : ""}
      ${state ? `<p class="verification-freshness" data-state="${state}" aria-live="polite">${label(state)}</p>` : ""}
    </section>`;
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) void refresh(true); });
  window.addEventListener("focus", () => void refresh(true));
  window.setInterval(() => { if (!document.hidden && !busy) void refresh(); }, 5000);
  return {card, changed, refresh};
})();
