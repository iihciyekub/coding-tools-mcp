const HOST = "com.codingtoolsmcp.chrome_bridge";
let nativePort = null;

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
      const tabId = Number(params.tabId);
      const target = { tabId };
      await chrome.debugger.attach(target, "1.3");
      try {
        const response = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
          expression: String(params.script || ""),
          awaitPromise: true,
          returnByValue: true
        });
        if (response.exceptionDetails) {
          throw new Error(response.exceptionDetails.text || "JavaScript evaluation failed");
        }
        result = response.result ? response.result.value : null;
      } finally {
        await chrome.debugger.detach(target).catch(() => {});
      }
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
