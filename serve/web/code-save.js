const extensions = new Map(Object.entries({
  bash: "sh", c: "c", cpp: "cpp", "c++": "cpp", csharp: "cs", cs: "cs", css: "css",
  go: "go", html: "html", java: "java", javascript: "js", js: "js", json: "json",
  jsx: "jsx", markdown: "md", md: "md", php: "php", powershell: "ps1", py: "py",
  python: "py", rs: "rs", rust: "rs", sh: "sh", sql: "sql", svg: "svg", toml: "toml",
  ts: "ts", tsx: "tsx", typescript: "ts", xml: "xml", yaml: "yaml", yml: "yml",
}));
const copySelector = ".st-msg--assistant [data-code-copy]";

export function installCodeSaving(chat, projectFolder, notify) {
  if (typeof window.showSaveFilePicker !== "function") return;
  let saving = false;

  function addButton(copy) {
    if (copy.closest(".st-bubble.cursor") || copy.parentElement.querySelector("[data-save-code]")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "st-btn st-btn--secondary";
    button.dataset.saveCode = "";
    button.textContent = "Save code";
    copy.before(button);
  }

  chat.querySelectorAll(copySelector).forEach(addButton);
  new MutationObserver(records => {
    for (const { addedNodes } of records) {
      for (const node of addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        if (node.matches(copySelector)) addButton(node);
        node.querySelectorAll(copySelector).forEach(addButton);
      }
    }
  }).observe(chat, { childList: true, subtree: true });

  chat.addEventListener("click", async event => {
    const button = event.target.closest("[data-save-code]");
    if (!event.isTrusted || !button || saving) return;
    const folder = projectFolder();
    if (!folder) {
      notify("info", "Choose a project folder before saving code.");
      return;
    }
    const block = button.closest(".st-code");
    const code = block.querySelector("pre code").textContent;
    const language = block.querySelector(".st-code__head > span").textContent.trim().toLowerCase();
    saving = true;
    button.disabled = true;
    try {
      const file = await window.showSaveFilePicker({
        suggestedName: `code.${extensions.get(language) ?? "txt"}`,
        ...(!folder.token ? {startIn: folder} : {}),
      });
      // The picker grants access to the file the person approved. The project handle is only a starting location.
      const writable = await file.createWritable();
      await new Blob([code], { type: "text/plain;charset=utf-8" }).stream().pipeTo(writable);
      notify("info", "Code saved", file.name);
    } catch (error) {
      if (error.name === "NotAllowedError" || error.name === "SecurityError") {
        notify("error", "Could not save code", "Check whether Chrome is blocking file editing for this site.");
      } else if (error.name !== "AbortError") {
        notify("error", "Could not save code", "The browser could not write to the selected file.");
      }
    } finally {
      saving = false;
      button.disabled = false;
    }
  });
}
