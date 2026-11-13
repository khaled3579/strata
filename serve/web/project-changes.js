"use strict";

const ProjectChanges = (() => {
  const dialog = document.getElementById("project-changes");
  const body = document.getElementById("project-changes-body");
  const status = document.getElementById("project-changes-status");
  const undo = document.getElementById("project-changes-undo");
  const labels = {
    view: ["View changes", "عرض التغييرات"], close: ["Close", "إغلاق"],
    undo: ["Undo this reply’s changes", "التراجع عن تعديلات هذا الرد"],
    confirm: ["Confirm undo", "تأكيد التراجع"], cancel: ["Cancel", "إلغاء"],
    confirmText: ["Restore these files to their state before this reply?", "استعادة حالة هذه الملفات قبل هذا الرد؟"],
    loading: ["Loading changes…", "جارٍ تحميل التغييرات…"],
    undone: ["Changes undone", "تم التراجع عن التعديلات"],
    added: ["Added", "إضافة"], removed: ["Removed", "حذف"], modified: ["Modified", "تعديل"],
    noDiff: ["Text diff unavailable", "المقارنة النصية غير متاحة"],
    reconnect: ["Reconnect the project to view this change record.", "أعد ربط المشروع لعرض سجل التغييرات."],
  };
  let selection, confirming = false, pending = false;
  const arabic = message => /[\u0600-\u06ff]/.test(message.verificationLanguage || "");
  const label = (key, ar) => labels[key][ar ? 1 : 0];

  function button(message) {
    return `<button type="button" class="st-btn st-btn--secondary project-changes-button"
      data-project-change="${esc(message.changeId)}">${label("view", arabic(message))}</button>`;
  }

  async function request(action, selected) {
    const folder = window.projectFolderForChat?.();
    if (!folder || folder.path !== selected.verificationProject) throw Error(label("reconnect", arabic(selected)));
    const response = await fetch(`project/${action}`, {
      method: "POST", headers: headers(true),
      body: JSON.stringify({token: folder.token, change_id: selected.changeId}),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error?.message || response.statusText);
    return result;
  }

  function render(report, ar) {
    body.replaceChildren();
    for (const file of report.files) {
      const row = document.createElement("details");
      const title = document.createElement("summary");
      const name = document.createElement("bdi");
      name.textContent = file.path;
      title.append(name, ` · ${label(file.status, ar)}`);
      row.append(title);
      if (file.diff) {
        const diff = document.createElement("pre");
        diff.dir = "ltr";
        for (const line of file.diff.split("\n")) {
          const text = document.createElement("span");
          if (/^[+-](?![+-]{2})/.test(line)) text.className = line[0] === "+" ? "change-add" : "change-remove";
          text.textContent = line + "\n";
          diff.append(text);
        }
        row.append(diff);
      } else {
        const note = document.createElement("p");
        note.textContent = label("noDiff", ar);
        row.append(note);
      }
      row.open = report.files.length === 1;
      body.append(row);
    }
    status.textContent = report.undone ? label("undone", ar) : report.problem || "";
    undo.hidden = !report.can_undo;
    undo.textContent = label("undo", ar);
  }

  async function open(message) {
    if (pending || busy) return;
    selection = message;
    const ar = arabic(message);
    confirming = false;
    dialog.dir = ar ? "rtl" : "ltr";
    $("project-changes-title").textContent = label("view", ar);
    $("project-changes-close").textContent = label("close", ar);
    body.replaceChildren();
    status.textContent = label("loading", ar);
    undo.hidden = true;
    dialog.showModal();
    try {
      const report = await request("changes", message);
      if (selection === message && dialog.open) render(report, ar);
    } catch (error) {
      if (selection === message && dialog.open) status.textContent = error.message;
    }
  }

  undo.addEventListener("click", async () => {
    if (!selection || busy || pending) return;
    const ar = arabic(selection);
    if (!confirming) {
      confirming = true;
      undo.textContent = label("confirm", ar);
      $("project-changes-close").textContent = label("cancel", ar);
      status.textContent = label("confirmText", ar);
      return;
    }
    pending = undo.disabled = true;
    try {
      const report = await request("undo", selection);
      render(report, ar);
      ProjectVerification.changed();
      void ProjectVerification.refresh(true);
      window.refreshProjectTree?.();
    } catch (error) {
      status.textContent = error.message;
      undo.textContent = label("undo", ar);
    } finally {
      confirming = pending = undo.disabled = false;
      $("project-changes-close").textContent = label("close", ar);
    }
  });

  document.getElementById("chat").addEventListener("click", event => {
    const key = event.target.closest("[data-project-change]")?.dataset.projectChange;
    const message = key && messages.find(item => item.changeId === key);
    if (message) void open(message);
  });
  return {button};
})();
