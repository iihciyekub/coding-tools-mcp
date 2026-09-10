const HOST = "com.codingtoolsmcp.chrome_bridge";
let nativePort = null;
const tabQueues = new Map();

async function executeInTab(tabId, script, expiresAt) {
  const previous = tabQueues.get(tabId) || Promise.resolve();
  const current = previous.catch(() => {}).then(async () => {
    if (Date.now() >= expiresAt) throw new Error("Chrome operation timed out while waiting for the tab");
    const target = { tabId };
    await chrome.debugger.attach(target, "1.3");
    try {
      const timeout = Math.max(0, expiresAt - Date.now() - 25);
      if (!timeout) throw new Error("Chrome operation timed out while attaching to the tab");
      const expression = `(async () => {
        let timer;
        try {
          return await Promise.race([
            Promise.resolve().then(() => (0, eval)(${JSON.stringify(script)})),
            new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("Chrome operation timed out")), ${timeout}); })
          ]);
        } finally { clearTimeout(timer); }
      })()`;
      const response = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
        expression, awaitPromise: true, returnByValue: true, timeout
      });
      if (response.exceptionDetails) {
        throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text || "JavaScript evaluation failed");
      }
      return response.result ? response.result.value : null;
    } finally {
      await chrome.debugger.detach(target).catch(() => {});
    }
  });
  tabQueues.set(tabId, current);
  try { return await current; }
  finally { if (tabQueues.get(tabId) === current) tabQueues.delete(tabId); }
}

function serializable(value) {
  if (value === undefined) return null;
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (_) {
    return String(value);
  }
}

async function handle(request) {
  const { id, action, params = {} } = request || {};
  try {
    let result;
    if (action === "status") {
      result = {
        extensionId: chrome.runtime.id,
        version: chrome.runtime.getManifest().version,
        name: chrome.runtime.getManifest().name,
        connected: true
      };
    } else if (action === "extensions") {
      const extensions = await chrome.management.getAll();
      result = extensions.map((item) => ({
        id: item.id,
        name: item.name,
        version: item.version,
        enabled: item.enabled,
        type: item.type,
        installType: item.installType
      }));
    } else if (action === "tabs") {
      const tabs = await chrome.tabs.query({});
      result = tabs.map((tab) => ({
        id: tab.id,
        windowId: tab.windowId,
        active: tab.active,
        highlighted: tab.highlighted,
        pinned: tab.pinned,
        title: tab.title,
        url: tab.url,
        status: tab.status
      }));
    } else if (action === "execute") {
      result = await executeInTab(Number(params.tabId), String(params.script || ""),
        Date.now() + Math.max(1, Math.min(120000, Number(params.timeoutMs) || 5000)));
    } else if (action === "send") {
      result = await chrome.runtime.sendMessage(String(params.extensionId || ""), params.message);
    } else {
      throw new Error(`Unsupported action: ${action}`);
    }
    nativePort?.postMessage({ id, ok: true, result: serializable(result) });
  } catch (error) {
    nativePort?.postMessage({ id, ok: false, error: String(error?.message || error) });
  }
}

function connect() {
  try {
    nativePort = chrome.runtime.connectNative(HOST);
    nativePort.onMessage.addListener((message) => void handle(message));
    nativePort.onDisconnect.addListener(() => {
      nativePort = null;
      setTimeout(connect, 1500);
    });
  } catch (_) {
    nativePort = null;
    setTimeout(connect, 1500);
  }
}

connect();
