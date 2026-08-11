const statusEl = document.getElementById("status");

function setStatus(text) {
  statusEl.textContent = text;
}

// The grid may live in a cross-origin iframe (e.g. The Times embeds its
// puzzle from a separate feeds.thetimes.com frame), and content.js now runs
// generically in every frame of every page. chrome.tabs.sendMessage without
// a frameId only reaches the top frame, so broadcast to every frame and use
// whichever one actually detected a grid.
async function sendToActiveTab(message) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const frames = await chrome.webNavigation.getAllFrames({ tabId: tab.id });

  const results = await Promise.all(
    frames.map((f) =>
      chrome.tabs.sendMessage(tab.id, message, { frameId: f.frameId }).catch(() => null)
    )
  );

  const ok = results.find((r) => r && r.ok);
  if (ok) return ok;
  const anyResponse = results.find((r) => r !== null);
  if (anyResponse) return anyResponse;
  throw new Error("No frame on this page responded (content script may not be injected here).");
}

document.getElementById("scan").addEventListener("click", async () => {
  setStatus("Scanning...");
  try {
    const res = await sendToActiveTab({ action: "scan" });
    const n = res.result.childCountCandidates.length;
    const k = res.result.keywordMatches.length;
    const auto = res.result.autoDetected ? `Auto-detected: ${res.result.autoDetected}. ` : "Not auto-detected. ";
    setStatus(`${auto}${n} child-count candidate(s), ${k} keyword match(es). Full JSON logged to the page console (F12).`);
  } catch (err) {
    setStatus("Error: " + err.message);
  }
});

document.getElementById("logger").addEventListener("click", async () => {
  try {
    const res = await sendToActiveTab({ action: "toggleLogger" });
    setStatus(res.logging ? "Logging started. Click an empty cell and press a digit on the page, then click this again to stop, then check the console." : "Logging stopped. Check the page console (F12).");
  } catch (err) {
    setStatus("Error: " + err.message);
  }
});

document.getElementById("solve").addEventListener("click", async () => {
  setStatus("Solving...");
  try {
    const res = await sendToActiveTab({ action: "solve" });
    setStatus(res.ok ? `Solved (${res.strategy}).` : "Error: " + res.error);
  } catch (err) {
    setStatus("Error: " + err.message);
  }
});

document.getElementById("hint").addEventListener("click", async () => {
  setStatus("Getting hint...");
  try {
    const res = await sendToActiveTab({ action: "hint" });
    setStatus(res.ok ? `Filled one cell (${res.strategy}).` : "Error: " + res.error);
  } catch (err) {
    setStatus("Error: " + err.message);
  }
});
