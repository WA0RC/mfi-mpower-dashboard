#!/usr/bin/env python3
"""
mPower Pro port monitor - small self-contained Flask webserver.

Serves a live dashboard of all 8 outlet ports (state, voltage, current,
power draw) on port 8081. Refresh happens client-side via a configurable
interval dropdown or a manual refresh button; every refresh triggers a
fresh, read-only SSH query against the device (no polling loop on the
server side, so nothing runs when nobody's looking at the page).

Usage:
    ~/mficlient/bin/pip3 install flask asyncssh
    ~/mficlient/bin/python3 dashboard.py
    # then visit http://<pi-ip>:8081/
"""

import asyncio
import json
import os
import threading
import time
from collections import deque
import asyncssh
from flask import Flask, jsonify, request, Response

# --- Device connection settings ---------------------------------------
DEFAULT_DEVICE_CONFIG = {
    "host": "10.0.0.2",
    "username": "ubnt",
    "password": "ubnt",
}

FIELDS = ["output", "relay", "active_pwr", "v_rms", "i_rms"]
PORTS = range(1, 9)

SSH_OPTIONS = dict(
    kex_algs="diffie-hellman-group1-sha1",
    encryption_algs="aes128-cbc",
    server_host_key_algs="ssh-rsa",
    known_hosts=None,
)

# --- Per-port labels/tooltips, loaded from config.json next to this file ---
# Edit config.json (created on first run if missing) to rename ports and add
# hover tooltips - no restart needed, it's re-read on every request.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "device": dict(DEFAULT_DEVICE_CONFIG),
    **{str(i): {"label": f"Port {i}", "tooltip": "", "enabled": True} for i in PORTS},
}


def ensure_config_exists():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)


def load_config():
    """Read config.json fresh each call. Falls back to defaults on any problem."""
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "device": dict(DEFAULT_DEVICE_CONFIG),
            "ports": {str(i): {"label": f"Port {i}", "tooltip": ""} for i in PORTS},
        }

    raw = raw if isinstance(raw, dict) else {}

    device_entry = raw.get("device", {}) if isinstance(raw.get("device", {}), dict) else {}
    device_config = {
        "host": device_entry.get("host") or DEFAULT_DEVICE_CONFIG["host"],
        "username": device_entry.get("username") or DEFAULT_DEVICE_CONFIG["username"],
        "password": device_entry.get("password") or DEFAULT_DEVICE_CONFIG["password"],
    }

    ports = {}
    for i in PORTS:
        key = str(i)
        entry = raw.get(key, {}) if isinstance(raw.get(key, {}), dict) else {}
        enabled_value = entry.get("enabled", True)
        ports[key] = {
            "label": entry.get("label") or f"Port {i}",
            "tooltip": entry.get("tooltip") or "",
            "enabled": enabled_value if isinstance(enabled_value, bool) else True,
        }

    return {
        "device": device_config,
        "ports": ports,
    }


ensure_config_exists()
CONFIG = load_config()
HOST = CONFIG["device"]["host"]
USERNAME = CONFIG["device"]["username"]
PASSWORD = CONFIG["device"]["password"]

app = Flask(__name__)

# --- Power history, kept in memory and shared by every viewer ---------
# One deque per port, appended once per poll (manual or interval-triggered).
# Since polling only happens when someone has the page open, this tracks
# "power over the last N polls", not wall-clock time - which is what you
# want when polling only happens on demand.
HISTORY_MAXLEN = 60
_history = {i: deque(maxlen=HISTORY_MAXLEN) for i in PORTS}

# --- Persistent SSH connection, kept alive on a dedicated background loop ---
# Flask is synchronous and asyncssh is async, so one long-lived asyncio event
# loop runs in a background thread. The SSH connection is opened once and
# reused across requests instead of reconnecting (and redoing the legacy DH
# handshake) on every single refresh. If the connection is ever found closed
# (device reboot, network blip, idle timeout) it's transparently reopened.

_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()

_conn = None
_conn_lock = asyncio.Lock()


async def _get_connection():
    """Return a live SSH connection, (re)connecting if necessary."""
    global _conn
    async with _conn_lock:
        if _conn is None or _conn.is_closed():
            _conn = await asyncssh.connect(
                host=HOST, username=USERNAME, password=PASSWORD, **SSH_OPTIONS
            )
        return _conn


async def read_power_data():
    """Read all port fields over the persistent connection, reconnecting once on failure."""
    data = {field: {} for field in FIELDS}

    async def run_all(conn):
        for field in FIELDS:
            cmd = " ; ".join(
                f"echo -n '{field}{i}=' ; cat /proc/power/{field}{i}" for i in PORTS
            )
            result = await conn.run(cmd)
            for line in result.stdout.strip().splitlines():
                key, _, value = line.partition("=")
                port_num = int(key.replace(field, ""))
                data[field][port_num] = value.strip()

    conn = await _get_connection()
    try:
        await run_all(conn)
    except (asyncssh.Error, OSError, EOFError):
        # connection died between fetch and use - drop it and retry once, fresh
        global _conn
        async with _conn_lock:
            _conn = None
        conn = await _get_connection()
        await run_all(conn)

    config = load_config()
    ports_cfg = config["ports"]
    ports = []
    for i in PORTS:
        power_w = float(data["active_pwr"].get(i, 0) or 0)
        _history[i].append(power_w)
        cfg = ports_cfg[str(i)]
        ports.append({
            "port": i,
            "label": cfg["label"],
            "tooltip": cfg["tooltip"],
            "enabled": cfg.get("enabled", True),
            "on": data["output"].get(i) == "1",
            "power_w": power_w,
            "voltage_v": float(data["v_rms"].get(i, 0) or 0),
            "current_a": float(data["i_rms"].get(i, 0) or 0),
            "history": list(_history[i]),
        })
    return ports


def run_on_loop(coro, timeout=15):
    """Submit a coroutine to the background event loop and block for its result."""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


async def set_port_state(port: int, state: int):
    """Turn a port on (state=1) or off (state=0).

    Uses 'echo <state> | tee /proc/power/outputN' rather than shell '>'
    redirection - this device's non-interactive SSH exec silently blocks '>'
    redirection and reports a fixed exit code, but piping into tee works.
    """
    cmd = f"echo {state} | tee /proc/power/output{port}"

    async def run_it(conn):
        result = await conn.run(cmd)
        if result.exit_status != 0:
            raise RuntimeError(f"device returned exit code {result.exit_status}")

    conn = await _get_connection()
    try:
        await run_it(conn)
    except (asyncssh.Error, OSError, EOFError):
        global _conn
        async with _conn_lock:
            _conn = None
        conn = await _get_connection()
        await run_it(conn)


@app.route("/api/status")
def api_status():
    try:
        ports = run_on_loop(read_power_data())
        return jsonify({"ok": True, "ports": ports})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/control", methods=["POST"])
def api_control():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        port = int(payload.get("port"))
        state = int(payload.get("state"))
        if port not in PORTS:
            raise ValueError(f"port must be 1-8, got {port}")
        if state not in (0, 1):
            raise ValueError(f"state must be 0 or 1, got {state}")
        config = load_config()
        if not config["ports"].get(str(port), {}).get("enabled", True):
            raise PermissionError(f"control disabled for port {port}")
        run_on_loop(set_port_state(port, state))
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>mPower Pro Monitor</title>
<style>
  :root {
    --bg: #14181D;
    --panel: #1C222A;
    --panel-border: #2A323B;
    --card: #1E252D;
    --card-border: #2C343D;
    --text: #E8EAED;
    --text-dim: #838E99;
    --text-faint: #5B6570;
    --amber: #E8A33D;
    --amber-dim: #4A3E28;
    --off: #414A53;
    --warn: #D9603D;
    --mono: "SF Mono", "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
    --sans: -apple-system, "Segoe UI", Inter, Roboto, sans-serif;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    padding: 18px 24px;
    background: var(--panel);
    border-bottom: 1px solid var(--panel-border);
  }

  .title-block h1 {
    margin: 0;
    font-size: 24px;
    font-weight: 700;
    letter-spacing: 0.01em;
  }

  .title-block .host {
    margin-top: 2px;
    font-family: var(--mono);
    font-size: 14px;
    color: var(--text-faint);
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .controls label {
    font-size: 15px;
    color: var(--text-dim);
  }

  select {
    background: var(--card);
    color: var(--text);
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 15px;
    font-family: var(--sans);
    cursor: pointer;
  }

  select:focus-visible, button:focus-visible {
    outline: 2px solid var(--amber);
    outline-offset: 1px;
  }

  button.refresh {
    background: var(--card);
    color: var(--text);
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 15px;
    font-family: var(--sans);
    cursor: pointer;
    transition: border-color 0.15s ease;
  }

  button.refresh:hover {
    border-color: var(--amber);
  }

  .status-line {
    padding: 10px 24px;
    font-size: 14px;
    color: var(--text-faint);
    font-family: var(--mono);
  }

  .status-line.error {
    color: var(--warn);
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-rows: repeat(4, minmax(0, 1fr));
    gap: 16px;
    padding: 8px 24px 28px;
    max-width: none;
    flex: 1 1 auto;
    min-height: 0;
    height: calc(100vh - 150px);
  }

  .port-card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 10px;
    padding: 20px 18px;
    min-height: 170px;
    height: 100%;
    display: flex;
    flex-direction: column;
    transition: border-color 0.2s ease;
  }

  .port-card.on {
    border-color: var(--amber-dim);
  }

  .port-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
    position: relative;
  }

  .port-title {
    display: flex;
    flex-direction: column;
    gap: 1px;
    position: relative;
  }

  .port-label {
    font-size: 20px;
    font-weight: 800;
    color: var(--text);
    letter-spacing: 0.01em;
    cursor: default;
  }

  .port-num {
    font-size: 16px;
    font-weight: 800;
    color: var(--amber);
    font-family: var(--mono);
    letter-spacing: 0.06em;
  }

  .tooltip-bubble {
    position: absolute;
    bottom: calc(100% + 8px);
    left: 0;
    max-width: 220px;
    background: var(--panel);
    border: 1px solid var(--card-border);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 12px;
    line-height: 1.4;
    color: var(--text);
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.35);
    opacity: 0;
    pointer-events: none;
    transform: translateY(4px);
    transition: opacity 0.12s ease, transform 0.12s ease;
    z-index: 10;
  }

  .port-title:hover .tooltip-bubble {
    opacity: 1;
    transform: translateY(0);
  }

  .spark-row {
    display: flex;
    align-items: stretch;
    gap: 12px;
    margin: 16px 0 8px;
    min-width: 0;
  }

  .sparkline-shell {
    flex: 1 1 auto;
    min-width: 0;
  }

  .sparkline {
    display: block;
    width: 100%;
    height: 72px;
    margin: 0;
    flex: 0 0 auto;
  }

  .sparkline path.fill {
    fill: var(--amber-dim);
    fill-opacity: 0.35;
    stroke: none;
  }

  .sparkline path.line {
    fill: none;
    stroke: var(--amber);
    stroke-width: 1.5;
  }

  .port-card:not(.on) .sparkline path.line {
    stroke: var(--text-faint);
  }

  .port-card:not(.on) .sparkline path.fill {
    fill: var(--off);
    fill-opacity: 0.25;
  }

  .indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 24px;
    font-weight: 700;
  }

  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--off);
  }

  .port-card.on .dot {
    background: var(--amber);
    box-shadow: 0 0 6px var(--amber);
  }

  .indicator .label-off { color: var(--text-faint); }
  .port-card.on .indicator .label-on { color: var(--amber); }
  .indicator .label-on { display: none; }
  .port-card.on .indicator .label-on { display: inline; }
  .port-card.on .indicator .label-off { display: none; }

  .measurements {
    flex: 0 0 120px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 8px;
  }

  .reading {
    font-family: var(--mono);
    display: flex;
    justify-content: flex-end;
    align-items: baseline;
    gap: 4px;
    padding: 0;
  }

  .reading .unit {
    font-size: 14px;
    color: var(--text-faint);
  }

  .reading .value {
    font-size: 18px;
    color: var(--text);
  }

  .reading.power .value {
    font-size: 26px;
    font-weight: 700;
  }

  .port-card:not(.on) .reading .value {
    color: var(--text-faint);
  }

  .divider {
    height: 1px;
    background: var(--card-border);
    margin: 10px 0 12px;
  }

  .toggle-row {
    display: flex;
    gap: 8px;
    margin-top: auto;
  }

  .control-disabled-banner {
    margin-top: auto;
    padding: 8px 10px;
    border-radius: 6px;
    border: 1px solid var(--card-border);
    background: rgba(255, 255, 255, 0.02);
    color: var(--text-faint);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-align: center;
    opacity: 0.8;
  }

  .toggle-btn {
    flex: 1 1 0;
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.04em;
    border-radius: 6px;
    padding: 9px 0;
    border: 1px solid var(--card-border);
    background: transparent;
    transition: border-color 0.15s ease, background-color 0.15s ease, opacity 0.15s ease;
  }

  .toggle-btn.on-btn {
    color: var(--amber);
    border-color: var(--amber-dim);
    cursor: pointer;
  }

  .toggle-btn.on-btn:not(:disabled):hover {
    border-color: var(--amber);
    background: var(--amber-dim);
  }

  .toggle-btn.off-btn {
    color: var(--warn);
    border-color: #4A2F26;
    cursor: pointer;
  }

  .toggle-btn.off-btn:not(:disabled):hover {
    border-color: var(--warn);
    background: rgba(217, 96, 61, 0.14);
  }

  .toggle-btn:disabled {
    cursor: not-allowed;
    opacity: 0.32;
    color: var(--text-faint);
    border-color: var(--card-border);
    background: transparent;
  }

  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; }
  }
</style>
</head>
<body>

<div class="topbar">
  <div class="title-block">
    <h1>mPower Pro Monitor</h1>
    <div class="host">10.0.0.252 &middot; 8 ports &middot; labels in config.json</div>
  </div>
  <div class="controls">
    <label for="interval">Refresh every</label>
    <select id="interval">
      <option value="0">Manual</option>
      <option value="1">1s</option>
      <option value="2">2s</option>
      <option value="5" selected>5s</option>
      <option value="10">10s</option>
      <option value="30">30s</option>
      <option value="60">60s</option>
    </select>
    <button class="refresh" id="refreshBtn">Refresh now</button>
  </div>
</div>

<div class="status-line" id="statusLine">Loading&hellip;</div>

<div class="grid" id="grid"></div>

<script>
let timer = null;

function buildSparkline(history) {
  const w = 220, h = 62;
  if (!history || history.length < 2) {
    return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"></svg>`;
  }
  const max = Math.max(...history, 0.001);
  const stepX = w / (history.length - 1);
  const points = history.map((v, idx) => {
    const x = idx * stepX;
    const y = h - (v / max) * (h - 2) - 1;
    return [x, y];
  });
  const linePath = points.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  const fillPath = linePath + ` L${w},${h} L0,${h} Z`;
  return `
    <svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="fill" d="${fillPath}"></path>
      <path class="line" d="${linePath}"></path>
    </svg>`;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function renderPorts(ports) {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  ports.forEach(p => {
    const card = document.createElement('div');
    card.className = 'port-card' + (p.on ? ' on' : '');
    const tooltipHtml = p.tooltip
      ? `<div class="tooltip-bubble">${escapeHtml(p.tooltip)}</div>`
      : '';
    card.innerHTML = `
      <div class="port-head">
        <div class="port-title">
          <span class="port-label">${escapeHtml(p.label)}</span>
          <span class="port-num">Port ${p.port}</span>
          ${tooltipHtml}
        </div>
        <span class="indicator">
          <span class="dot"></span>
          <span class="label-on">ON</span>
          <span class="label-off">OFF</span>
        </span>
      </div>
      <div class="reading power">
        <span class="value">${p.power_w.toFixed(1)}</span>
        <span class="unit">W</span>
      </div>
      <div class="spark-row">
        <div class="sparkline-shell">
          ${buildSparkline(p.history)}
        </div>
        <div class="measurements">
          <div class="reading">
            <span class="value">${p.voltage_v.toFixed(1)}</span>
            <span class="unit">V</span>
          </div>
          <div class="reading">
            <span class="value">${p.current_a.toFixed(3)}</span>
            <span class="unit">A</span>
          </div>
        </div>
      </div>
      <div class="divider"></div>
      ${p.enabled ? `
        <div class="toggle-row">
          <button class="toggle-btn on-btn" data-port="${p.port}" data-state="1" ${p.on ? 'disabled' : ''}>ON</button>
          <button class="toggle-btn off-btn" data-port="${p.port}" data-state="0" ${!p.on ? 'disabled' : ''}>OFF</button>
        </div>
      ` : `
        <div class="control-disabled-banner">control disabled</div>
      `}
    `;
    grid.appendChild(card);
  });
}

async function controlPort(port, state, btn) {
  const statusLine = document.getElementById('statusLine');
  btn.disabled = true;
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port, state })
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'Unknown error');
    await refresh();
  } catch (err) {
    statusLine.className = 'status-line error';
    statusLine.textContent = 'Failed to switch port ' + port + ': ' + err.message;
    btn.disabled = false;
  }
}

document.getElementById('grid').addEventListener('click', (e) => {
  const btn = e.target.closest('.toggle-btn');
  if (!btn || btn.disabled) return;
  const port = parseInt(btn.dataset.port, 10);
  const state = parseInt(btn.dataset.state, 10);
  controlPort(port, state, btn);
});

async function refresh() {
  const statusLine = document.getElementById('statusLine');
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'Unknown error');
    renderPorts(data.ports);
    statusLine.className = 'status-line';
    statusLine.textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch (err) {
    statusLine.className = 'status-line error';
    statusLine.textContent = 'Failed to reach device: ' + err.message;
  }
}

function setupInterval() {
  if (timer) clearInterval(timer);
  const seconds = parseInt(document.getElementById('interval').value, 10);
  if (seconds > 0) {
    timer = setInterval(refresh, seconds * 1000);
  }
}

document.getElementById('interval').addEventListener('change', setupInterval);
document.getElementById('refreshBtn').addEventListener('click', refresh);

refresh();
setupInterval();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    # threaded=True lets Flask accept a new request while another is still
    # waiting on the (serialized, single-connection) SSH round trip
    app.run(host="0.0.0.0", port=8081, threaded=True)
