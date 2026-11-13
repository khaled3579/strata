"use strict";

const ProjectCheckSettings = (() => {
  const panel = document.getElementById("project-check-settings");
  const button = document.getElementById("project-check-settings-button");
  const commands = document.getElementById("project-check-command-list");
  const notice = document.getElementById("project-check-settings-status");
  const form = document.getElementById("project-check-settings-form");
  const save = document.getElementById("project-check-save");
  const reset = document.getElementById("project-check-reset");
  const add = document.getElementById("project-check-add");
  const labels = {
    title: ["Check settings", "إعدادات الفحص"],
    close: ["Close", "إغلاق"],
    detected: ["Detected commands", "الأوامر المكتشفة"],
    add: ["Add command", "إضافة أمر"],
    reset: ["Use detected commands", "استخدام الأوامر المكتشفة"],
    save: ["Save", "حفظ"],
    loading: ["Loading commands…", "جارٍ تحميل الأوامر…"],
    empty: ["No build or test commands detected.", "لم تُكتشف أوامر بناء أو اختبار."],
    argv: ["Tool and arguments (JSON array)", "الأداة والوسائط (مصفوفة JSON)"],
    cwd: ["Working folder", "مجلد التشغيل"],
    remove: ["Remove", "إزالة"],
    invalid: ["Enter each command as a JSON array, for example [\"npm\", \"test\"].",
              "أدخل كل أمر كمصفوفة JSON، مثل [\"npm\", \"test\"]."],
    saved: ["Saved. Run the checks again to use these commands.", "حُفظت الإعدادات. أعد تشغيل الفحص لاستخدام هذه الأوامر."],
  };
  let selected, version, pending = false;
  let automatic = true;
  let ar = /^ar\b/i.test(navigator.language);
  const label = key => labels[key][ar ? 1 : 0];

  function sync(enabled, working) {
    button.hidden = !enabled;
    button.disabled = working || pending;
    const latest = messages.findLast(message => message.role === "user");
    ar = /^ar\b/i.test(navigator.language) || /[\u0600-\u06ff]/.test(latest?.text || "");
    button.textContent = label("title");
  }

  function row({argv = [], cwd = "."} = {}) {
    const node = document.createElement("div");
    node.className = "project-check-command";
    const field = (key, value, className) => {
      const wrapper = document.createElement("label");
      const input = document.createElement("input");
      input.className = "st-input " + className;
      input.dir = "ltr";
      input.required = true;
      input.value = value;
      wrapper.append(label(key), input);
      node.append(wrapper);
    };
    field("argv", JSON.stringify(argv), "check-argv");
    field("cwd", cwd, "check-cwd");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "st-btn st-btn--secondary";
    remove.textContent = label("remove");
    remove.onclick = () => node.remove();
    node.append(remove);
    commands.append(node);
  }

  function show(data) {
    version = data.version;
    automatic = data.automatic;
    commands.replaceChildren();
    data.commands.forEach(row);
    const list = $("project-check-detected-list");
    list.replaceChildren();
    for (const {argv, cwd} of data.detected) {
      const item = document.createElement("li");
      const code = document.createElement("code");
      code.dir = "ltr";
      code.textContent = JSON.stringify(argv);
      item.append(code);
      if (cwd !== ".") item.append(" · ", cwd);
      list.append(item);
    }
    if (!data.detected.length) {
      const item = document.createElement("li");
      item.textContent = label("empty");
      list.append(item);
    }
    reset.disabled = automatic;
    notice.textContent = data.error || "";
  }

  async function request(update) {
    const response = await fetch("project/check-settings", {
      method: "POST", headers: headers(true),
      body: JSON.stringify({token: selected.token, update}),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error?.message || response.statusText);
    return data;
  }

  async function load(update) {
    pending = true;
    form.inert = true;
    notice.textContent = label("loading");
    try {
      show(await request(update));
      if (update) {
        notice.textContent = label("saved");
        ProjectVerification.changed();
        void ProjectVerification.refresh(true);
        window.refreshProjectTree?.();
      }
    } catch (error) {
      notice.textContent = error.message;
    } finally {
      pending = form.inert = false;
      button.disabled = !!busy;
      save.disabled = add.disabled = !version;
    }
  }

  button.addEventListener("click", () => {
    if (busy || pending) return;
    selected = window.projectFolderForChat?.();
    if (!selected) return;
    version = null;
    panel.dir = ar ? "rtl" : "ltr";
    $("project-check-settings-title").textContent = label("title");
    $("project-check-settings-close").textContent = label("close");
    $("project-check-settings-folder").textContent = selected.path;
    $("project-check-detected-title").textContent = label("detected");
    $("project-check-detected-list").replaceChildren();
    $("project-check-detected").open = true;
    commands.replaceChildren();
    reset.disabled = true;
    add.textContent = label("add");
    reset.textContent = label("reset");
    save.textContent = label("save");
    panel.showModal();
    void load();
  });
  add.addEventListener("click", () => { if (commands.childElementCount < 16) row(); });
  reset.addEventListener("click", () => { if (!pending && version && !automatic) void load({version, automatic: true}); });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (pending || !version) return;
    let rows;
    try {
      rows = Array.from(commands.children, item => ({
        argv: JSON.parse(item.querySelector(".check-argv").value),
        cwd: item.querySelector(".check-cwd").value.trim(),
      }));
      if (!rows.length || rows.some(({argv}) => !Array.isArray(argv) || !argv.length || argv.some(v => typeof v !== "string"))) {
        throw Error();
      }
    } catch {
      notice.textContent = label("invalid");
      return;
    }
    void load({version, commands: rows});
  });
  return {sync};
})();
