/* AirPlay to Bluetooth Bridge — Web UI controller
 * Refactored for Home Assistant Ingress compatibility:
 *   - Pure relative API paths via window.API_BASE (no leading slashes)
 *   - Client-side tab switching via DOM visibility only
 *   - Global error handlers with visible diagnostic overlay
 *   - Safe event binding after DOM readiness check
 */

// ============================================================ GLOBAL ERROR HANDLERS
window.onerror = function (msg, src, line, col, err) {
  console.error("[Bridge UI] Global error:", msg, "at", src + ":" + line + ":" + col, err);
  showErrorOverlay(msg + "\n\nSource: " + src + "\nLine: " + line + ":" + col + "\n\n" + (err && err.stack ? err.stack : ""));
  return true;
};

window.onunhandledrejection = function (event) {
  var reason = event && event.reason ? event.reason : event;
  console.error("[Bridge UI] Unhandled promise rejection:", reason);
  showErrorOverlay("Unhandled promise rejection:\n\n" + (reason && reason.stack ? reason.stack : String(reason)));
};

function showErrorOverlay(detail) {
  var overlay = document.getElementById("errorOverlay");
  var pre = document.getElementById("errorDetail");
  if (!overlay || !pre) return;
  pre.textContent = detail || "Unknown error";
  overlay.style.display = "block";
}

// ============================================================ MAIN INIT
function initBridgeUI() {
  "use strict";

  console.log("[Bridge UI] Initializing…");

  // ----------------------------------------------------- helpers
  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  var API_BASE = window.API_BASE || "/";

  function apiPath(sub) {
    // sub must NOT start with a leading slash
    return API_BASE + sub;
  }

  async function api(sub, opts) {
    opts = opts || {};
    var fetchOpts = {
      headers: { "Content-Type": "application/json" },
    };
    if (opts.method) fetchOpts.method = opts.method;
    if (opts.body) fetchOpts.body = opts.body;
    try {
      var res = await fetch(apiPath(sub), fetchOpts);
      var text = await res.text();
      try {
        return JSON.parse(text);
      } catch (e) {
        console.error("[Bridge UI] Non-JSON response from", sub, ":", text.substring(0, 200));
        return { status: "error", message: "Invalid response from server" };
      }
    } catch (e) {
      console.error("[Bridge UI] Fetch failed for", sub, ":", e.message);
      return { status: "error", message: e.message };
    }
  }

  function statusClass(status) {
    var normalized = String(status || "IDLE").toLowerCase();
    if (normalized === "connected" || normalized === "bridged" || normalized === "bridged (streaming)") return "status-connected";
    if (normalized === "buffering" || normalized === "connecting") return "status-buffering";
    if (normalized === "disconnected") return "status-disconnected";
    return "status-idle";
  }

  function setStatusBadge(id, status) {
    var el = $("#" + id);
    if (!el) return;
    var value = String(status || "IDLE").toUpperCase();
    el.textContent = value;
    el.className = "status-badge " + statusClass(value);
  }

  function renderTopology(data) {
    var devices = data.bluetooth_devices || [];
    var instances = data.airplay_instances || [];
    var connected = devices.find(function (device) { return device.connected; });
    var instance = instances.find(function (item) { return item.status === "BRIDGED" || item.status === "BUFFERING"; }) || instances[0];
    var engineReady = data.system_audio && data.system_audio.default_sink;
    var activeStatus = instance ? instance.status : "IDLE";
    var source = instance && instance.active_client ? instance.active_client : "No active client";
    var receiver = instance ? instance.name + (instance.port ? " · Port " + instance.port : "") : "No receiver running";
    var speaker = connected ? connected.name + " · " + connected.mac : "No connected speaker";
    var sink = instance && instance.mapped_sink ? instance.mapped_sink : (connected && connected.active_sink ? connected.active_sink : "No A2DP sink yet");

    $("#topologySource").textContent = source;
    $("#topologyReceiver").textContent = receiver;
    $("#topologyEngine").textContent = engineReady ? data.system_audio.default_sink : "Waiting for PipeWire sink";
    $("#topologySpeaker").textContent = speaker;
    setStatusBadge("topologySourceBadge", instance && instance.active_client ? "BRIDGED" : "IDLE");
    setStatusBadge("topologyReceiverBadge", activeStatus);
    setStatusBadge("topologyEngineBadge", engineReady ? "CONNECTED" : "BUFFERING");
    setStatusBadge("topologySpeakerBadge", connected ? "CONNECTED" : "DISCONNECTED");
    setStatusBadge("airplayPanelBadge", activeStatus);
    setStatusBadge("systemAudioBadge", engineReady ? "CONNECTED" : "BUFFERING");
    $("#airplayReceiverName").textContent = instance ? instance.name : "—";
    $("#airplayListenPort").textContent = instance && instance.port ? String(instance.port) : "—";
    $("#airplayClient").textContent = source;
    $("#airplayTarget").textContent = sink;
    $("#systemDefaultSink").textContent = data.system_audio && data.system_audio.default_sink || "—";
    $("#systemVolume").textContent = data.system_audio && data.system_audio.volume != null ? data.system_audio.volume + "%" : "—";
    $("#systemMute").textContent = data.system_audio && data.system_audio.muted != null ? (data.system_audio.muted ? "Muted" : "Unmuted") : "—";
    $("#overviewUpdated").textContent = "Updated " + new Date().toLocaleTimeString();
  }

  async function loadTopology() {
    var data = await api("api/status");
    if (data.status === "ok") renderTopology(data);
  }

  function renderNowPlaying(data) {
    var tracks = data.tracks || [];
    var track = tracks[0];
    if (!track) {
      $("#nowPlayingTitle").textContent = "No track playing";
      $("#nowPlayingArtist").textContent = "—";
      $("#nowPlayingAlbum").textContent = "—";
      $("#nowPlayingStatus").textContent = "—";
      $("#nowPlayingCover").innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-8 h-8 text-slate-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>';
      setStatusBadge("nowPlayingBadge", "IDLE");
      return;
    }
    $("#nowPlayingTitle").textContent = track.title || "Unknown";
    $("#nowPlayingArtist").textContent = track.artist || "—";
    $("#nowPlayingAlbum").textContent = track.album || "—";
    $("#nowPlayingStatus").textContent = track.status || "playing";
    setStatusBadge("nowPlayingBadge", track.status || "CONNECTED");
    if (track.cover_art && track.cover_art.length > 100) {
      $("#nowPlayingCover").innerHTML = '<img src="data:image/png;base64,' + track.cover_art + '" class="w-full h-full object-cover" alt="cover">';
    } else {
      $("#nowPlayingCover").innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-8 h-8 text-slate-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>';
    }
  }

  async function loadNowPlaying() {
    var data = await api("api/now-playing");
    if (data.status === "ok") renderNowPlaying(data);
  }

  function toast(message, type) {
    type = type || "ok";
    var el = $("#toast");
    var inner = $("#toastInner");
    if (!el || !inner) return;
    var colors = {
      ok: "bg-emerald-600 text-white",
      error: "bg-red-600 text-white",
      info: "bg-sky-600 text-white",
    };
    inner.className = "px-5 py-3 rounded-xl shadow-2xl font-medium text-sm " + (colors[type] || colors.ok);
    inner.textContent = message;
    el.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.classList.add("hidden"); }, 3500);
  }

  function rssiColor(rssi) {
    if (rssi >= -50) return "text-emerald-400";
    if (rssi >= -70) return "text-amber-400";
    return "text-red-400";
  }

  function badge(text, cls) {
    return '<span class="badge ' + cls + '">' + text + '</span>';
  }

  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // ----------------------------------------------------- tab switching
  var tabButtons = $$(".nav-tab");
  var tabContents = $$(".tab-content");

  console.log("[Bridge UI] Found", tabButtons.length, "tab buttons,", tabContents.length, "tab sections");

  tabButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var tabName = btn.getAttribute("data-tab");
      console.log("[Bridge UI] Tab clicked:", tabName);

      tabButtons.forEach(function (b) {
        b.classList.remove("active");
        b.classList.remove("border-sky-400", "text-sky-300");
        b.classList.add("border-transparent", "text-slate-400");
      });
      btn.classList.add("active");
      btn.classList.add("border-sky-400", "text-sky-300");
      btn.classList.remove("border-transparent", "text-slate-400");

      tabContents.forEach(function (c) {
        c.style.display = "none";
      });

      var target = $("#tab-" + tabName);
      if (target) {
        target.style.display = "block";
      } else {
        console.error("[Bridge UI] No tab content found for:", tabName);
      }

      if (tabName === "speakers") {
        console.log("[Bridge UI] Loading speakers…");
        loadSpeakers();
      }
      if (tabName === "diagnostics") {
        console.log("[Bridge UI] Loading diagnostics…");
        loadLogs();
        loadBtStatus();
        loadSinks();
        loadBtEvents();
      }
    });
  });

  console.log("[Bridge UI] Tab handlers bound");

  // ----------------------------------------------------- TAB 1: scan
  var scanning = false;
  var scanBtn = $("#btnScan");

  if (scanBtn) {
    scanBtn.addEventListener("click", async function () {
      console.log("[Bridge UI] Scan button clicked");
      if (scanning) return;
      scanning = true;
      scanBtn.disabled = true;
      var span = scanBtn.querySelector("span");
      if (span) span.textContent = "Scanning…";
      $("#scanProgress").classList.remove("hidden");
      $("#scanResults").innerHTML = "";
      $("#scanEmpty").classList.add("hidden");

      var countdown = 10;
      $("#scanCountdown").textContent = countdown + "s";
      var interval = setInterval(function () {
        countdown--;
        $("#scanCountdown").textContent = countdown + "s";
        if (countdown <= 0) clearInterval(interval);
      }, 1000);

      try {
        var data = await api("api/scan", { method: "POST", body: JSON.stringify({ duration: 10 }) });
        console.log("[Bridge UI] Scan response:", data.status, (data.devices || []).length, "devices");
        if (data.status === "ok" && data.devices && data.devices.length) {
          renderScanResults(data.devices);
        } else {
          $("#scanEmpty").classList.remove("hidden");
          var p = $("#scanEmpty").querySelector("p");
          if (p) p.textContent = "No devices found. Try moving your speaker closer.";
        }
      } catch (e) {
        console.error("[Bridge UI] Scan error:", e);
        toast("Scan failed: " + e.message, "error");
      } finally {
        scanning = false;
        scanBtn.disabled = false;
        if (span) span.textContent = "Scan for Bluetooth Devices";
        $("#scanProgress").classList.add("hidden");
        clearInterval(interval);
      }
    });
    console.log("[Bridge UI] Scan button bound");
  } else {
    console.error("[Bridge UI] Scan button not found!");
  }

  function renderScanResults(devices) {
    var container = $("#scanResults");
    container.innerHTML = "";
    devices
      .slice()
      .sort(function (a, b) { return b.rssi - a.rssi; })
      .forEach(function (dev) {
        var card = document.createElement("div");
        card.className = "device-card p-4 rounded-xl bg-slate-900 border border-slate-800 flex items-center justify-between gap-4";
        card.innerHTML =
          '<div class="flex items-center gap-3 min-w-0">' +
            '<div class="w-10 h-10 rounded-lg bg-slate-800 flex items-center justify-center flex-shrink-0">' +
              '<svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5 text-sky-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7l10 10-5 5V2l5 5L7 17"/></svg>' +
            '</div>' +
            '<div class="min-w-0">' +
              '<p class="font-medium text-slate-100 truncate">' + escapeHtml(dev.name) + '</p>' +
              '<p class="text-xs text-slate-500 font-mono">' + escapeHtml(dev.mac) + '</p>' +
            '</div>' +
          '</div>' +
          '<div class="flex items-center gap-3 flex-shrink-0">' +
            '<span class="text-xs ' + rssiColor(dev.rssi) + ' font-mono">' + (dev.rssi || "?") + ' dBm</span>' +
            (dev.paired ? badge("Paired", "badge-green") : "") +
            '<button class="pair-btn px-3 py-1.5 rounded-lg text-sm font-medium bg-sky-500 hover:bg-sky-400 text-white transition-colors active:scale-95"' +
              ' data-mac="' + escapeHtml(dev.mac) + '" data-name="' + escapeHtml(dev.name) + '">Pair &amp; Configure</button>' +
          '</div>';
        container.appendChild(card);
      });

    $$(".pair-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        console.log("[Bridge UI] Pair button clicked for", btn.dataset.mac);
        openPairModal(btn.dataset.mac, btn.dataset.name);
      });
    });
  }

  // ----------------------------------------------------- pairing mode
  var pairingModeActive = false;
  var pairingCountdownInterval = null;
  var pairingBtn = $("#btnPairingMode");

  if (pairingBtn) {
    pairingBtn.addEventListener("click", async function () {
      console.log("[Bridge UI] Pairing mode button clicked");
      if (pairingModeActive) {
        console.log("[Bridge UI] Pairing mode already active, stopping");
        stopPairingMode();
        return;
      }
      pairingBtn.disabled = true;
      var span = pairingBtn.querySelector("span");
      if (span) span.textContent = "Enabling…";
      try {
        var data = await api("api/bluetooth/discoverable", { method: "POST", body: JSON.stringify({ duration: 180 }) });
        console.log("[Bridge UI] Discoverable response:", data.status);
        if (data.status === "ok") {
          startPairingCountdown(data.remaining || 180);
          toast("Pairing mode active for 3 minutes — ask Alexa to connect Bluetooth", "ok");
        } else {
          toast(data.message || "Failed to enable pairing mode", "error");
        }
      } catch (e) {
        console.error("[Bridge UI] Pairing mode error:", e);
        toast("Pairing mode error: " + e.message, "error");
      } finally {
        pairingBtn.disabled = false;
        if (span && !pairingModeActive) span.textContent = "Enable Pairing Mode (3 min)";
      }
    });
    console.log("[Bridge UI] Pairing mode button bound");
  } else {
    console.error("[Bridge UI] Pairing mode button not found!");
  }

  function startPairingCountdown(seconds) {
    pairingModeActive = true;
    var span = pairingBtn.querySelector("span");
    if (span) span.textContent = "Stop Pairing Mode";
    $("#pairingModeInfo").classList.remove("hidden");
    $("#pairingInstructions").classList.remove("hidden");

    var remaining = seconds;
    updateCountdownDisplay(remaining);

    pairingCountdownInterval = setInterval(function () {
      remaining--;
      updateCountdownDisplay(remaining);
      if (remaining <= 0) {
        clearInterval(pairingCountdownInterval);
        pairingCountdownInterval = null;
        pairingModeActive = false;
        if (span) span.textContent = "Enable Pairing Mode (3 min)";
        $("#pairingModeInfo").classList.add("hidden");
        $("#pairingInstructions").classList.add("hidden");
        toast("Pairing mode ended", "info");
        loadSpeakers();
      } else if (remaining % 15 === 0) {
        // Auto-refresh speakers list while pairing mode is active.
        loadSpeakers();
      }
    }, 1000);
  }

  function updateCountdownDisplay(secs) {
    var m = Math.floor(secs / 60);
    var s = secs % 60;
    var el = $("#pairingCountdown");
    if (el) el.textContent = (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  async function stopPairingMode() {
    if (pairingCountdownInterval) {
      clearInterval(pairingCountdownInterval);
      pairingCountdownInterval = null;
    }
    pairingModeActive = false;
    var span = pairingBtn.querySelector("span");
    if (span) span.textContent = "Enable Pairing Mode (3 min)";
    $("#pairingModeInfo").classList.add("hidden");
    $("#pairingInstructions").classList.add("hidden");
    try {
      await api("api/bluetooth/discoverable", { method: "DELETE" });
      toast("Pairing mode stopped", "info");
    } catch (e) {
      console.error("[Bridge UI] Stop pairing error:", e);
    }
    loadSpeakers();
  }

  // Check if pairing mode is already active on page load (e.g. after refresh).
  async function checkPairingMode() {
    try {
      var data = await api("api/bluetooth/discoverable");
      if (data.discoverable && data.remaining > 0) {
        console.log("[Bridge UI] Pairing mode already active, remaining:", data.remaining);
        startPairingCountdown(data.remaining);
      }
    } catch (e) { /* ignore */ }
  }

  // ----------------------------------------------------- pair modal
  var pairMac = "";

  function openPairModal(mac, name) {
    pairMac = mac;
    $("#pairModalDesc").textContent = 'Configure "' + name + '" (' + mac + ') as an AirPlay speaker.';
    var input = $("#pairNameInput");
    input.value = name && name !== mac ? name : "";
    input.placeholder = "e.g. Wohnzimmer Sound";
    $("#pairModal").classList.remove("hidden");
    $("#pairModal").classList.add("flex");
    input.focus();
  }

  function closePairModal() {
    $("#pairModal").classList.add("hidden");
    $("#pairModal").classList.remove("flex");
  }

  var pairCancelBtn = $("#pairCancel");
  if (pairCancelBtn) pairCancelBtn.addEventListener("click", function () { console.log("[Bridge UI] Pair cancelled"); closePairModal(); });

  var pairConfirmBtn = $("#pairConfirm");
  if (pairConfirmBtn) {
    pairConfirmBtn.addEventListener("click", async function () {
      console.log("[Bridge UI] Pair confirm clicked for", pairMac);
      var name = $("#pairNameInput").value.trim();
      if (!name) {
        toast("Please enter a custom AirPlay name", "error");
        return;
      }
      closePairModal();
      toast("Pairing " + pairMac + "…", "info");
      try {
        var data = await api("api/pair", {
          method: "POST",
          body: JSON.stringify({ mac: pairMac, name: name }),
        });
        console.log("[Bridge UI] Pair response:", data.status);
        if (data.status === "ok") {
          toast('Paired! AirPlay receiver "' + name + '" is now live.', "ok");
        } else {
          toast(data.message || "Pairing failed", "error");
        }
      } catch (e) {
        console.error("[Bridge UI] Pair error:", e);
        toast("Pairing error: " + e.message, "error");
      }
    });
  }

  // ----------------------------------------------------- TAB 2: speakers
  async function loadSpeakers() {
    console.log("[Bridge UI] loadSpeakers()");
    try {
      var data = await api("api/speakers");
      var list = data.speakers || [];
      var container = $("#speakersList");
      container.innerHTML = "";
      if (!list.length) {
        $("#speakersEmpty").classList.remove("hidden");
        return;
      }
      $("#speakersEmpty").classList.add("hidden");
      list.forEach(function (spk) {
        var card = document.createElement("div");
        card.className = "device-card p-4 rounded-xl bg-slate-900 border border-slate-800";
        var connBadge = spk.connected ? badge("Connected", "badge-green") : badge("Disconnected", "badge-slate");
        var streamBadge = spk.streaming ? badge("Streaming", "badge-sky") : badge("Idle", "badge-slate");
        var airplayBadge = spk.airplay_active ? badge("AirPlay On", "badge-green") : badge("AirPlay Off", "badge-red");
        card.innerHTML =
          '<div class="flex items-start justify-between gap-4 mb-3">' +
            '<div class="min-w-0">' +
              '<div class="flex items-center gap-2 mb-1">' +
                '<p class="font-semibold text-slate-100 text-lg truncate">' + escapeHtml(spk.name) + '</p>' +
                airplayBadge +
              '</div>' +
              '<p class="text-xs text-slate-500 font-mono">' + escapeHtml(spk.mac) + '</p>' +
            '</div>' +
            '<div class="flex flex-col items-end gap-1 flex-shrink-0">' +
              '<div class="flex gap-1">' + connBadge + ' ' + streamBadge + '</div>' +
              '<span class="text-xs text-slate-500">' + (spk.port ? "Port " + spk.port : "") + '</span>' +
            '</div>' +
          '</div>' +
          '<div class="flex flex-wrap gap-2 pt-2 border-t border-slate-800">' +
            '<button class="edit-btn px-3 py-1.5 rounded-lg text-sm bg-slate-800 hover:bg-slate-700 text-slate-300 transition-colors"' +
              ' data-mac="' + escapeHtml(spk.mac) + '" data-name="' + escapeHtml(spk.name) + '">Edit Name</button>' +
            '<button class="test-btn px-3 py-1.5 rounded-lg text-sm bg-sky-600/80 hover:bg-sky-500 text-white transition-colors"' +
              ' data-mac="' + escapeHtml(spk.mac) + '">Test Connect</button>' +
            (spk.airplay_on
              ? '<button class="airplay-stop-btn px-3 py-1.5 rounded-lg text-sm bg-orange-600/80 hover:bg-orange-500 text-white transition-colors" data-mac="' + escapeHtml(spk.mac) + '">Stop AirPlay</button>'
              : '<button class="airplay-start-btn px-3 py-1.5 rounded-lg text-sm bg-emerald-600/80 hover:bg-emerald-500 text-white transition-colors" data-mac="' + escapeHtml(spk.mac) + '">Start AirPlay</button>'
            ) +
            '<button class="unpair-btn px-3 py-1.5 rounded-lg text-sm bg-amber-600/80 hover:bg-amber-500 text-white transition-colors"' +
              ' data-mac="' + escapeHtml(spk.mac) + '" data-name="' + escapeHtml(spk.name) + '">Unpair</button>' +
            '<button class="delete-btn px-3 py-1.5 rounded-lg text-sm bg-red-600/80 hover:bg-red-500 text-white transition-colors"' +
              ' data-mac="' + escapeHtml(spk.mac) + '" data-name="' + escapeHtml(spk.name) + '">Delete</button>' +
          '</div>';
        container.appendChild(card);
      });

      document.querySelectorAll(".edit-btn").forEach(function (b) { b.addEventListener("click", function () { openEditModal(b.dataset.mac, b.dataset.name); }); });
      document.querySelectorAll(".test-btn").forEach(function (b) { b.addEventListener("click", function () { testConnect(b.dataset.mac); }); });
      document.querySelectorAll(".airplay-start-btn").forEach(function (b) { b.addEventListener("click", function () { airplayStart(b.dataset.mac); }); });
      document.querySelectorAll(".airplay-stop-btn").forEach(function (b) { b.addEventListener("click", function () { airplayStop(b.dataset.mac); }); });
      document.querySelectorAll(".unpair-btn").forEach(function (b) { b.addEventListener("click", function () { unpairSpeaker(b.dataset.mac, b.dataset.name); }); });
      document.querySelectorAll(".delete-btn").forEach(function (b) { b.addEventListener("click", function () { deleteSpeaker(b.dataset.mac, b.dataset.name); }); });
    } catch (e) {
      console.error("[Bridge UI] loadSpeakers error:", e);
      toast("Failed to load speakers: " + e.message, "error");
    }
  }

  async function testConnect(mac) {
    console.log("[Bridge UI] testConnect:", mac);
    toast("Testing connection to " + mac + "…", "info");
    try {
      var data = await api("api/speakers/" + mac + "/test-connect", { method: "POST" });
      toast(data.message, data.status === "ok" ? "ok" : "error");
      loadSpeakers();
    } catch (e) {
      toast("Test failed: " + e.message, "error");
    }
  }

  async function airplayStart(mac) {
    console.log("[Bridge UI] airplayStart:", mac);
    toast("Starting AirPlay for " + mac + "…", "info");
    try {
      var data = await api("api/speakers/" + mac + "/airplay/start", { method: "POST" });
      toast(data.message, data.status === "ok" ? "ok" : "error");
      loadSpeakers();
    } catch (e) {
      toast("Start failed: " + e.message, "error");
    }
  }

  async function airplayStop(mac) {
    console.log("[Bridge UI] airplayStop:", mac);
    toast("Stopping AirPlay for " + mac + "…", "info");
    try {
      var data = await api("api/speakers/" + mac + "/airplay/stop", { method: "POST" });
      toast(data.message, data.status === "ok" ? "ok" : "error");
      loadSpeakers();
    } catch (e) {
      toast("Stop failed: " + e.message, "error");
    }
  }

  async function deleteSpeaker(mac, name) {
    console.log("[Bridge UI] deleteSpeaker:", mac);
    if (!confirm('Delete "' + name + '" (' + mac + ')?\nThis will un-trust the MAC, stop its AirPlay daemon, and remove it from storage.')) return;
    try {
      var data = await api("api/speakers/" + mac, { method: "DELETE" });
      toast(data.message || "Speaker removed", "ok");
      loadSpeakers();
    } catch (e) {
      toast("Delete failed: " + e.message, "error");
    }
  }

  async function unpairSpeaker(mac, name) {
    console.log("[Bridge UI] unpairSpeaker:", mac);
    if (!confirm('Unpair "' + name + '" (' + mac + ')?\nThis will remove the device from Bluetooth completely so it can be freshly paired again.')) return;
    toast("Unpairing " + mac + "…", "info");
    try {
      var data = await api("api/bluetooth/unpair", { method: "POST", body: JSON.stringify({ mac: mac }) });
      toast(data.message || "Device unpaired", data.status === "ok" ? "ok" : "error");
      loadSpeakers();
    } catch (e) {
      toast("Unpair failed: " + e.message, "error");
    }
  }

  // ----------------------------------------------------- edit modal
  var editMac = "";

  function openEditModal(mac, name) {
    console.log("[Bridge UI] openEditModal:", mac);
    editMac = mac;
    $("#editNameInput").value = name;
    $("#editModal").classList.remove("hidden");
    $("#editModal").classList.add("flex");
    $("#editNameInput").focus();
  }

  var editCancelBtn = $("#editCancel");
  if (editCancelBtn) {
    editCancelBtn.addEventListener("click", function () {
      console.log("[Bridge UI] Edit cancelled");
      $("#editModal").classList.add("hidden");
      $("#editModal").classList.remove("flex");
    });
  }

  var editConfirmBtn = $("#editConfirm");
  if (editConfirmBtn) {
    editConfirmBtn.addEventListener("click", async function () {
      console.log("[Bridge UI] Edit confirm for", editMac);
      var name = $("#editNameInput").value.trim();
      if (!name) {
        toast("Name cannot be empty", "error");
        return;
      }
      try {
        var data = await api("api/speakers/" + editMac, {
          method: "PUT",
          body: JSON.stringify({ name: name }),
        });
        if (data.status === "ok") {
          toast("Name updated — AirPlay receiver restarting", "ok");
          $("#editModal").classList.add("hidden");
          $("#editModal").classList.remove("flex");
          loadSpeakers();
        } else {
          toast(data.message || "Update failed", "error");
        }
      } catch (e) {
        toast("Edit failed: " + e.message, "error");
      }
    });
  }

  // ----------------------------------------------------- TAB 3: diagnostics
  var logRefresh = null;

  async function loadLogs() {
    try {
      var data = await api("api/logs");
      var view = $("#logView");
      var logs = data.logs || [];
      view.innerHTML = logs.map(function (l) { return '<div class="log-line">' + escapeHtml(l) + '</div>'; }).join("");
      view.scrollTop = view.scrollHeight;
    } catch (e) { /* ignore */ }
    if (!logRefresh) logRefresh = setInterval(loadLogs, 2000);
  }

  async function loadBtStatus() {
    try {
      var data = await api("api/bluetooth/status");
      var a = data.adapter || {};
      var view = $("#btStatusView");
      if (!a.available) {
        view.textContent = "No Bluetooth adapter available.";
        return;
      }
      view.textContent =
        "Address:       " + (a.address || "N/A") + "\n" +
        "Name:          " + (a.name || "N/A") + "\n" +
        "Alias:         " + (a.alias || "N/A") + "\n" +
        "Powered:       " + (a.powered ? "YES" : "NO") + "\n" +
        "Discoverable:  " + (a.discoverable ? "YES" : "NO") + "\n" +
        "Discovering:   " + (a.discovering ? "YES" : "NO");
    } catch (e) {
      $("#btStatusView").textContent = "Error loading status: " + e.message;
    }
  }

  async function loadSinks() {
    try {
      var data = await api("api/audio/sinks");
      var pw = data.pipewire || {};
      var view = $("#sinksView");
      var text = "PipeWire:        " + (pw.pipewire_running || "?") + "\n" +
        "PipeWire-Pulse:  " + (pw.pipewire_pulse_running || "?") + "\n" +
        "WirePlumber:     " + (pw.wireplumber_running || "?") + "\n\n" +
        "--- Sinks ---\n" + (data.sinks || "(none)") + "\n\n" +
        "--- Cards ---\n" + (data.cards || "(none)") + "\n\n" +
        "--- Playback ---\n" + (data.playback || "(none)");
      view.textContent = text;
    } catch (e) {
      $("#sinksView").textContent = "Error: " + e.message;
    }
  }

  async function loadBtEvents() {
    try {
      var data = await api("api/bluetooth/events");
      var events = data.events || [];
      var view = $("#btEventsView");
      if (!events.length) {
        view.innerHTML = '<p class="text-xs text-slate-500">No Bluetooth events recorded yet.</p>';
        return;
      }
      view.innerHTML = events.slice().reverse().map(function (e) {
        var icon = e.success
          ? '<span class="text-emerald-400">OK</span>'
          : '<span class="text-red-400">FAIL</span>';
        var err = e.error_code ? ' <span class="text-red-400">[' + escapeHtml(e.error_code) + ']</span>' : "";
        return '<div class="text-xs font-mono p-2 rounded-lg bg-slate-950/50 border border-slate-800/50">' +
          '<span class="text-slate-500">' + escapeHtml(e.timestamp) + '</span>' +
          '<span class="text-sky-300 ml-2">' + escapeHtml(e.action) + '</span>' +
          '<span class="text-slate-400 ml-2">' + escapeHtml(e.mac) + '</span>' +
          '<span class="ml-2">' + icon + '</span>' + err +
        '</div>';
      }).join("");
    } catch (e) { /* ignore */ }
  }

  // ----------------------------------------------------- log level
  async function loadLogLevel() {
    try {
      var data = await api("api/log-level");
      highlightLogLevel(data.level);
    } catch (e) { /* ignore */ }
  }

  function highlightLogLevel(level) {
    $$(".log-level-btn").forEach(function (b) {
      if (b.getAttribute("data-level") === level) {
        b.classList.add("active");
      } else {
        b.classList.remove("active");
      }
    });
  }

  $$(".log-level-btn").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      var level = btn.getAttribute("data-level");
      console.log("[Bridge UI] Log level set to:", level);
      try {
        await api("api/log-level", { method: "PUT", body: JSON.stringify({ level: level }) });
        highlightLogLevel(level);
        toast("Log level set to " + level, "ok");
      } catch (e) {
        toast("Failed to set log level", "error");
      }
    });
  });

  // ----------------------------------------------------- diagnostic buttons
  var btnBtStatus = $("#btnBtStatus");
  if (btnBtStatus) btnBtStatus.addEventListener("click", function () { console.log("[Bridge UI] BT status refresh"); loadBtStatus(); });

  var btnSinks = $("#btnSinks");
  if (btnSinks) btnSinks.addEventListener("click", function () { console.log("[Bridge UI] Sinks refresh"); loadSinks(); });

  var btnBtEvents = $("#btnBtEvents");
  if (btnBtEvents) btnBtEvents.addEventListener("click", function () { console.log("[Bridge UI] BT events refresh"); loadBtEvents(); });

  var btnClearLogs = $("#btnClearLogs");
  if (btnClearLogs) {
    btnClearLogs.addEventListener("click", async function () {
      console.log("[Bridge UI] Clear logs clicked");
      await api("api/logs", { method: "DELETE" });
      loadLogs();
      toast("Logs cleared", "ok");
    });
  }

  var btnCopyLog = $("#btnCopyLog");
  if (btnCopyLog) {
    btnCopyLog.addEventListener("click", async function () {
      console.log("[Bridge UI] Copy system log clicked");
      try {
        var data = await api("api/logs");
        var logs = data.logs || [];
        var text = logs.join("\n");
        if (!text) {
          toast("Log is empty", "info");
          return;
        }
        try {
          await navigator.clipboard.writeText(text);
          toast("Log copied to clipboard!", "ok");
        } catch (clipErr) {
          // Fallback: create a temporary textarea for older browsers / non-secure contexts
          var ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.left = "-9999px";
          document.body.appendChild(ta);
          ta.select();
          try {
            document.execCommand("copy");
            toast("Log copied to clipboard!", "ok");
          } catch (execErr) {
            toast("Copy failed — your browser blocked clipboard access", "error");
          }
          document.body.removeChild(ta);
        }
      } catch (e) {
        toast("Failed to load logs for copy: " + e.message, "error");
      }
    });
    console.log("[Bridge UI] Copy log button bound");
  }

  var btnRestartDaemons = $("#btnRestartDaemons");
  if (btnRestartDaemons) {
    btnRestartDaemons.addEventListener("click", async function () {
      console.log("[Bridge UI] Restart daemons clicked");
      if (!confirm("Force restart all Shairport and PipeWire daemons? Active AirPlay streams will be interrupted.")) return;
      toast("Restarting daemons…", "info");
      try {
        var data = await api("api/daemons/restart", { method: "POST" });
        toast(data.message || "Daemons restarted", "ok");
      } catch (e) {
        toast("Restart failed: " + e.message, "error");
      }
    });
  }

  // ----------------------------------------------------- error overlay copy
  var errorCopyBtn = $("#errorCopyBtn");
  if (errorCopyBtn) {
    errorCopyBtn.addEventListener("click", function () {
      var detail = $("#errorDetail");
      if (detail && detail.textContent) {
        try {
          navigator.clipboard.writeText(detail.textContent);
          errorCopyBtn.textContent = "Copied!";
          setTimeout(function () { errorCopyBtn.textContent = "Copy to Clipboard"; }, 2000);
        } catch (e) {
          errorCopyBtn.textContent = "Copy failed";
        }
      }
    });
  }

  // ----------------------------------------------------- status dot
  async function checkStatus() {
    try {
      var data = await api("api/system/info");
      if (data.status === "ok") {
        $("#statusDot").className = "w-2.5 h-2.5 rounded-full bg-emerald-400";
        $("#statusText").textContent = "Running";
        $("#statusText").className = "text-sm text-emerald-300";
      }
    } catch (e) {
      $("#statusDot").className = "w-2.5 h-2.5 rounded-full bg-red-400";
      $("#statusText").textContent = "Offline";
    }
  }

  // ----------------------------------------------------- init
  console.log("[Bridge UI] All event listeners bound, starting polling");
  checkStatus();
  loadTopology();
  loadNowPlaying();
  loadLogLevel();
  checkPairingMode();
  setInterval(checkStatus, 10000);
  setInterval(loadTopology, 2000);
  setInterval(loadNowPlaying, 3000);
  setInterval(loadBtEvents, 5000);

  console.log("[Bridge UI] Initialization complete");
}

// ============================================================ DOM READINESS GUARD
try {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      try {
        initBridgeUI();
      } catch (e) {
        console.error("[Bridge UI] Init threw:", e);
        showErrorOverlay("Initialization error:\n\n" + (e.stack || e.message || String(e)));
      }
    });
  } else {
    initBridgeUI();
  }
} catch (e) {
  console.error("[Bridge UI] Top-level error:", e);
  showErrorOverlay("Fatal error:\n\n" + (e.stack || e.message || String(e)));
}
