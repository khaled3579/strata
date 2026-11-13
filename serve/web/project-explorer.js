import { installCodeSaving } from "./code-save.js";
import { projectFileActions, projectIcon } from "./project-file-actions.js";

(() => {
  "use strict";

  const explorer = document.getElementById("project-explorer");
  const chatView = document.getElementById("view-chat");
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
  const fileActions = projectFileActions(() => root?.handle, localRequest);
  const browserPicker = window.isSecureContext && typeof window.showDirectoryPicker === "function";
  let nativeMode = false;

  function chatFolder() {
    return listOn ? chat?.projectFolder : store.get("singleChatProject", null);
  }

  async function localRequest(action, parts, extra = {}) {
    const token = root?.handle.token;
    const response = await fetch(`project/${action}`, {
      method: "POST",
      headers: headers(true),
      body: JSON.stringify({token, parts, ...extra}),
    });
    if (!response.ok) {
      const result = await response.json();
      throw new Error(result.error?.message || "Could not access the project.");
    }
    return response;
  }

  window.projectFolderForChat = () => nativeMode ? chatFolder() : null;
  window.refreshProjectTree = refreshTree;
  window.syncProjectFolder = () => {
    if (!mcpInfo.project_picker) return;
    nativeMode = true;
    pick.disabled = folderAction || !!busy;
    const folder = chatFolder();
    if (root?.handle.token && folder?.token === root.handle.token) return;
    if (!folder && !root && status.textContent === "Choose a folder for this chat.") return;
    storageStatus.textContent = "";
    if (folder) {
      selectFolder({ ...folder, kind: "directory" }, true);
      queueMicrotask(() => void ProjectVerification.refresh(true));
      return;
    }
    root = null;
    entries.replaceChildren();
    fileActions.reconcile();
    name.hidden = true;
    refresh.disabled = true;
    reconnect.hidden = true;
    pick.title = "";
    pickLabel.textContent = "Choose folder";
    pick.setAttribute("aria-label", "Choose folder");
    status.textContent = "Choose a folder for this chat.";
  };

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
    return !panel.hidden && !chatView.hidden && !document.hidden;
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
    let handles = [];
    try {
      if (root?.handle.token) {
        const response = await localRequest("entries", branch.parts);
        ({entries: handles} = await response.json());
      } else {
        for await (const handle of branch.handle.values()) handles.push(handle);
      }
    } catch (error) {
      if (branch !== root && !branch.row?.isConnected) return;
      if (root?.handle.token) {
        branch.message.textContent = error.message;
        return;
      }
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
    if (handle?.kind !== "directory" || root || nativeMode) return;
    const permission = await handle.queryPermission(readOnly).catch(() => "prompt");
    if (!root && !nativeMode) selectFolder(handle, permission === "granted");
  }

  function setFolderAction(pending) {
    folderAction = pending;
    pick.disabled = pending || (nativeMode && !!busy);
    reconnect.disabled = pending;
  }

  pick.addEventListener("click", async event => {
    if (!event.isTrusted || folderAction || (nativeMode && busy)) return;
    setFolderAction(true);
    storageStatus.textContent = "";
    try {
      if (nativeMode) {
        const selectedChat = listOn ? chat : null;
        const singleChat = messages;
        const response = await localRequest("pick", []);
        const {folder} = await response.json();
        if (!folder) return;
        if (selectedChat && chats.includes(selectedChat)) {
          selectedChat.projectFolder = folder;
          chatStore.put(H.cleanChat(selectedChat));
        } else if (!listOn && !selectedChat && singleChat === messages) {
          store.set("singleChatProject", folder);
        }
        window.syncProjectFolder();
        return;
      }
      const handle = await window.showDirectoryPicker({ ...readOnly, id: "strata-project" });
      selectFolder(handle, true);
      storageStatus.textContent = "";
      try {
        await storedFolder(handle);
      } catch {
        storageStatus.textContent = "Could not remember this folder in this browser.";
      }
    } catch (error) {
      if (error.name !== "AbortError") storageStatus.textContent = error.message || "Could not open the selected folder.";
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
  if (!browserPicker && !mcpInfo.project_picker) {
    pick.disabled = true;
    status.textContent = "Open Strata on localhost in a browser that supports folder access.";
  }
  window.syncProjectFolder();
  installCodeSaving(document.getElementById("chat"), () => root?.handle, window.toast);
  if (browserPicker && !nativeMode) void restoreFolder();
  document.addEventListener("visibilitychange", refreshTree);
  new MutationObserver(refreshTree).observe(chatView, { attributes: true, attributeFilter: ["hidden"] });
  window.setInterval(refreshTree, 2500);
})();
