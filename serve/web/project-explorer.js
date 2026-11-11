import { installCodeSaving } from "./code-save.js";
import { projectFileActions, projectIcon } from "./project-file-actions.js";

(() => {
  "use strict";

  const explorer = document.getElementById("project-explorer");
  const chat = document.getElementById("view-chat");
  const toggle = document.getElementById("project-toggle");
  const panel = document.getElementById("project-panel");
  const pick = document.getElementById("project-pick");
  const refresh = document.getElementById("project-refresh");
  const reconnect = document.getElementById("project-reconnect");
  const name = document.getElementById("project-name");
  const pickLabel = document.getElementById("project-pick-label");
  const status = document.getElementById("project-status");
  const storageStatus = document.getElementById("project-storage-status");
  const entries = document.getElementById("project-entries");
  const collator = new Intl.Collator(undefined, { numeric: true });
  const readOnly = { mode: "read" };
  let root;
  let activeRefresh;
  let folderAction = false;
  const fileActions = projectFileActions(() => root?.handle);

  async function storedFolder(handle) {
    const database = await new Promise((resolve, reject) => {
      const request = indexedDB.open("strata-project-explorer", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("selection");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    try {
      return await new Promise((resolve, reject) => {
        const transaction = database.transaction("selection", handle ? "readwrite" : "readonly");
        const store = transaction.objectStore("selection");
        const request = handle ? store.put(handle, "last-folder") : store.get("last-folder");
        transaction.oncomplete = () => resolve(request.result);
        transaction.onabort = () => reject(transaction.error);
      });
    } finally {
      database.close();
    }
  }

  function visible() {
    return !panel.hidden && !chat.hidden && !document.hidden;
  }

  function setOpen(open) {
    explorer.dataset.open = String(open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Collapse project explorer" : "Expand project explorer");
    panel.hidden = !open;
    if (open) refreshTree();
    else fileActions.closeMenu();
  }

  function createEntry(handle, parent) {
    const row = document.createElement("li");
    const directory = handle.kind === "directory";
    const label = document.createElement(directory ? "summary" : "button");
    label.className = directory ? "project-entry-name" : "project-entry-name project-file";
    label.title = handle.name;
    const filename = document.createElement("bdi");
    filename.textContent = handle.name;
    label.append(filename);
    const parts = [...parent.parts, handle.name];
    if (!directory) {
      label.type = "button";
      const group = document.createElement("div");
      group.className = "project-file-row";
      group.append(label);
      row.append(group);
      const file = { row, group, label, handle, parts };
      fileActions.bind(file);
      return file;
    }

    label.prepend(projectIcon("chevron", "project-entry-chevron"), projectIcon("folder", "project-folder-icon"));
    const details = document.createElement("details");
    const message = document.createElement("p");
    message.className = "project-status project-branch-status muted small";
    message.setAttribute("role", "status");
    const list = document.createElement("ul");
    list.className = "project-entries";
    details.append(label, message, list);
    row.append(details);
    const branch = { handle, row, details, message, list, parts, children: new Map() };
    details.addEventListener("toggle", () => {
      if (details.open && visible()) void refreshBranch(branch);
    });
    return branch;
  }

  function renderEntries(branch, handles) {
    handles.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "directory" ? -1 : 1;
      return collator.compare(a.name, b.name);
    });
    const next = new Map();
    for (const handle of handles) {
      const key = handle.kind + ":" + handle.name;
      const entry = branch.children.get(key) || createEntry(handle, branch);
      entry.handle = handle;
      next.set(key, entry);
    }
    for (const [key, entry] of branch.children) {
      if (!next.has(key)) entry.row.remove();
    }
    let position = branch.list.firstChild;
    for (const { row } of next.values()) {
      if (row === position) position = position.nextSibling;
      else branch.list.insertBefore(row, position);
    }
    branch.children = next;
    fileActions.reconcile();
  }

  async function readBranch(branch) {
    const handles = [];
    try {
      for await (const handle of branch.handle.values()) handles.push(handle);
    } catch (error) {
      if (branch !== root && !branch.row?.isConnected) return;
      if (error.name === "NotAllowedError" || error.name === "NotFoundError") {
        renderEntries(branch, []);
        if (branch === root && error.name === "NotAllowedError") {
          setFolderAccess(false);
        } else {
          branch.message.textContent = branch === root
            ? "Folder unavailable. Choose it again or select another folder."
            : "This folder is unavailable.";
        }
      } else {
        branch.message.textContent = "Could not refresh this folder. Trying again automatically.";
      }
      return;
    }
    if (branch !== root && !branch.row?.isConnected) return;
    renderEntries(branch, handles);
    branch.message.textContent = handles.length ? "" : "This folder is empty.";
  }

  function refreshBranch(branch) {
    if (!branch.pending) {
      branch.pending = readBranch(branch).finally(() => { branch.pending = null; });
    }
    return branch.pending;
  }

  async function refreshExpanded(branch, selected) {
    if (root !== selected || !visible() || (branch.details && !branch.details.open)) return;
    await refreshBranch(branch);
    for (const child of branch.children.values()) {
      if (child.details?.open) await refreshExpanded(child, selected);
    }
  }

  function refreshTree() {
    if (!root?.connected || !visible() || activeRefresh === root) return;
    const selected = root;
    activeRefresh = selected;
    void refreshExpanded(selected, selected).finally(() => {
      if (activeRefresh === selected) activeRefresh = null;
    });
  }

  function setFolderAccess(connected) {
    root.connected = connected;
    reconnect.hidden = connected;
    refresh.disabled = !connected;
    status.textContent = connected ? "Loading folder…" : "Reconnect to view the saved folder.";
    if (connected) refreshTree();
  }

  function selectFolder(handle, connected) {
    entries.replaceChildren();
    fileActions.reconcile();
    root = { handle, list: entries, message: status, parts: [], children: new Map() };
    name.hidden = false;
    pick.title = handle.name;
    name.textContent = handle.name;
    pickLabel.textContent = "Change folder";
    pick.setAttribute("aria-label", "Change folder");
    setFolderAccess(connected);
  }

  async function restoreFolder() {
    let handle;
    try {
      handle = await storedFolder();
    } catch {
      if (!root) storageStatus.textContent = "Could not restore a saved folder in this browser.";
      return;
    }
    if (handle?.kind !== "directory" || root) return;
    const permission = await handle.queryPermission(readOnly).catch(() => "prompt");
    if (!root) selectFolder(handle, permission === "granted");
  }

  function setFolderAction(busy) {
    folderAction = busy;
    pick.disabled = busy;
    reconnect.disabled = busy;
  }

  pick.addEventListener("click", async () => {
    if (folderAction) return;
    setFolderAction(true);
    try {
      const handle = await window.showDirectoryPicker({ ...readOnly, id: "strata-project" });
      selectFolder(handle, true);
      storageStatus.textContent = "";
      try {
        await storedFolder(handle);
      } catch {
        storageStatus.textContent = "Could not remember this folder in this browser.";
      }
    } catch (error) {
      if (error.name !== "AbortError") status.textContent = "Could not open the selected folder.";
    } finally {
      setFolderAction(false);
    }
  });

  reconnect.addEventListener("click", async () => {
    if (folderAction || !root || root.connected) return;
    setFolderAction(true);
    try {
      const permission = await root.handle.requestPermission(readOnly);
      if (permission === "granted") setFolderAccess(true);
      else status.textContent = "Read access was not granted. Reconnect or choose another folder.";
    } catch {
      status.textContent = "Could not reconnect. Try again or choose another folder.";
    } finally {
      setFolderAction(false);
    }
  });

  explorer.hidden = false;
  setOpen(!window.matchMedia("(max-width: 760px)").matches);
  toggle.addEventListener("click", () => setOpen(panel.hidden));
  refresh.addEventListener("click", refreshTree);
  if (!window.isSecureContext || typeof window.showDirectoryPicker !== "function") {
    pick.disabled = true;
    status.textContent = "Open Strata on localhost in a browser that supports folder access.";
    return;
  }
  installCodeSaving(document.getElementById("chat"), () => root?.handle, window.toast);
  void restoreFolder();
  document.addEventListener("visibilitychange", refreshTree);
  new MutationObserver(refreshTree).observe(chat, { attributes: true, attributeFilter: ["hidden"] });
  window.setInterval(refreshTree, 2500);
})();
