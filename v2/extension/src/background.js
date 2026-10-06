/**
 * MV3 service worker (Part 6B-B). Its ONLY job is to make the toolbar action open the
 * side panel directly - no popup, no content script, no other background logic. It
 * makes no network calls of any kind.
 */

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => {
    // Never silently swallow a real configuration problem, but this must never crash
    // the service worker or expose anything sensitive.
    console.error("Stock Research AI: failed to configure the side panel", error);
  });
