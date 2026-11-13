const images = new Map([
  ["png", "image/png"], ["jpg", "image/jpeg"], ["jpeg", "image/jpeg"],
  ["gif", "image/gif"], ["webp", "image/webp"], ["avif", "image/avif"], ["bmp", "image/bmp"],
]);
const codeExtensions = new Set("bat c cpp css go h html java js json jsx md mjs ps1 py rs sh sql svg toml ts tsx vue xml yaml yml".split(" "));
const extension = name => name.includes(".") ? name.split(".").at(-1).toLowerCase() : "";
const localOrigins = new Set(["http://127.0.0.1:8080", "http://localhost:8080"]);

export function projectIcon(name, extraClass = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", `st-icon st-icon--sm ${extraClass}`);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `web/sprite.svg#i-${name}`);
  svg.append(use);
  return svg;
}

export function projectFileActions(getFolder, localRequest) {
  const menu = document.getElementById("project-menu");
  const menuName = document.getElementById("project-menu-name");
  const dialog = document.getElementById("project-preview");
  const title = document.getElementById("project-preview-title");
  const path = document.getElementById("project-preview-path");
  const body = document.getElementById("project-preview-body");
  const note = document.getElementById("project-preview-note");
  let selected;
  let menuEntry;
  let previewEntry;
  let previewVersion = 0;
  let imageUrl;

  function select(entry) {
    if (selected) {
      delete selected.group.dataset.selected;
      selected.label.setAttribute("aria-pressed", "false");
    }
    selected = entry;
    if (!entry) return;
    entry.group.dataset.selected = "true";
    entry.label.setAttribute("aria-pressed", "true");
  }

  function closeMenu(returnFocus = false) {
    if (!menuEntry) return;
    const { more } = menuEntry;
    menu.hidden = true;
    menuEntry = null;
    more.setAttribute("aria-expanded", "false");
    if (returnFocus && more.isConnected) more.focus();
  }

  function openMenu(entry, x, y) {
    closeMenu();
    select(entry);
    menuEntry = entry;
    menuName.textContent = entry.handle.name;
    menu.hidden = false;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(6, Math.min(x, innerWidth - width - 6))}px`;
    menu.style.top = `${Math.max(6, Math.min(y, innerHeight - height - 6))}px`;
    entry.more.setAttribute("aria-expanded", "true");
    menu.querySelector("button").focus({ preventScroll: true });
  }

  function clearPreview() {
    previewVersion += 1;
    previewEntry = null;
    body.replaceChildren();
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
  }

  async function preview(entry) {
    closeMenu();
    select(entry);
    clearPreview();
    previewEntry = entry;
    const version = previewVersion;
    title.textContent = entry.handle.name;
    path.textContent = entry.parts.join(" / ");
    note.textContent = "Loading preview…";
    if (!dialog.open) dialog.showModal();

    try {
      const folder = getFolder();
      const file = folder?.token ? await (await localRequest("preview", entry.parts)).blob()
        : await entry.handle.getFile();
      const mime = images.get(extension(entry.handle.name));
      if (mime) {
        if (version !== previewVersion) return;
        if (file.size > 24 * 1024 * 1024) {
          note.textContent = "This image is too large to preview here.";
          return;
        }
        imageUrl = URL.createObjectURL(file.slice(0, file.size, mime));
        const img = document.createElement("img");
        img.alt = entry.handle.name;
        img.src = imageUrl;
        img.addEventListener("error", () => {
          if (version === previewVersion) note.textContent = "This image could not be decoded.";
        });
        body.append(img);
        note.textContent = "";
        return;
      }

      const limit = 256 * 1024;
      const data = new Uint8Array(await file.slice(0, limit).arrayBuffer());
      if (version !== previewVersion) return;
      const utf16 = data[0] === 255 && data[1] === 254 ? "utf-16le"
        : data[0] === 254 && data[1] === 255 ? "utf-16be" : null;
      let text;
      try {
        if (!utf16 && data.includes(0)) throw new Error();
        text = new TextDecoder(utf16 || "utf-8", { fatal: true }).decode(data, { stream: file.size > limit });
      } catch {
        note.textContent = "Preview is available for text files and supported images. Open this file in VS Code to inspect it.";
        return;
      }
      const pre = document.createElement("pre");
      pre.textContent = text;
      body.append(pre);
      note.textContent = file.size > limit ? "Showing the first 256 KiB." : text ? "" : "This file is empty.";
    } catch (error) {
      if (version === previewVersion) note.textContent = error.message || "This file is unavailable. Refresh the project and try again.";
    }
  }

  async function openInWindows(entry, action) {
    const folder = getFolder();
    try {
      if (folder?.token && entry.row.isConnected) {
        await localRequest("open", entry.parts, {action});
        return;
      }
      if (!folder || !localOrigins.has(location.origin)) {
        throw new Error("Windows actions are available from the local Strata web server.");
      }
      const parts = await folder.resolve(entry.handle);
      if (!parts || !entry.row.isConnected || getFolder() !== folder) {
        throw new Error("The file is no longer in the selected project.");
      }
      const file = await entry.handle.getFile();
      const digest = await crypto.subtle.digest("SHA-256", await file.slice(0, 65536).arrayBuffer());
      const fingerprint = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
      if (!entry.row.isConnected || getFolder() !== folder) return;
      let response;
      try {
        response = await fetch("http://127.0.0.1:8081/open-project-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action, root: folder.name, parts, size: file.size, modified: file.lastModified, fingerprint }),
          signal: AbortSignal.timeout(4000),
        });
      } catch {
        throw new Error("The local Windows file helper is unavailable.");
      }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not open the file.");
    } catch (error) {
      window.toast("error", "Could not open the file", error.message);
    }
  }

  document.addEventListener("pointerdown", event => {
    if (menuEntry && !menu.contains(event.target) && !menuEntry.more.contains(event.target)) closeMenu();
  });
  document.addEventListener("focusin", event => {
    if (menuEntry && !menu.contains(event.target) && !menuEntry.more.contains(event.target)) closeMenu();
  });
  window.addEventListener("resize", () => closeMenu(true));
  dialog.addEventListener("close", () => {
    if (!dialog.open) clearPreview();
  });
  menu.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    const buttons = [...menu.querySelectorAll("button")];
    const index = buttons.indexOf(document.activeElement);
    const next = { ArrowDown: (index + 1) % buttons.length, ArrowUp: (index - 1 + buttons.length) % buttons.length,
      Home: 0, End: buttons.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    buttons[next].focus();
  });
  menu.addEventListener("click", event => {
    const action = event.target.closest("button")?.dataset.projectAction;
    if (!event.isTrusted || !menuEntry || !action) return;
    const entry = menuEntry;
    closeMenu(true);
    if (action === "preview") void preview(entry);
    else void openInWindows(entry, action);
  });

  function bind(entry) {
    const { label, group, handle } = entry;
    const ext = extension(handle.name);
    group.dataset.fileType = images.has(ext) ? "image" : codeExtensions.has(ext) ? "code" : "other";
    label.setAttribute("aria-pressed", "false");
    label.prepend(projectIcon(group.dataset.fileType === "code" ? "file-code" : "file", "project-file-icon"));
    const more = document.createElement("button");
    entry.more = more;
    more.type = "button";
    more.className = "st-btn st-btn--icon project-file-menu";
    more.setAttribute("aria-label", `Actions for ${handle.name}`);
    more.setAttribute("aria-haspopup", "menu");
    more.setAttribute("aria-expanded", "false");
    more.append(projectIcon("more"));
    group.append(more);

    function openAtButton() {
      const rect = more.getBoundingClientRect();
      openMenu(entry, rect.right - 218, rect.bottom + 3);
    }
    label.addEventListener("click", () => void preview(entry));
    label.addEventListener("keydown", event => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        openAtButton();
      }
    });
    more.addEventListener("click", () => {
      if (menuEntry === entry) closeMenu(true);
      else openAtButton();
    });
    group.addEventListener("contextmenu", event => {
      event.preventDefault();
      if (event.clientX || event.clientY) openMenu(entry, event.clientX, event.clientY);
      else openAtButton();
    });
  }

  function reconcile() {
    if (menuEntry && !menuEntry.row.isConnected) closeMenu();
    if (selected && !selected.row.isConnected) select(null);
    if (previewEntry && !previewEntry.row.isConnected && dialog.open) dialog.close();
  }

  return { bind, reconcile, closeMenu };
}
