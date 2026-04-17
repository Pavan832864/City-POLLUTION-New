"""
Smart City Pollution Monitoring System
Fog Node – Layer 2

Responsibilities:
  1. Expose an HTTP endpoint to receive raw sensor readings.
  2. Validate and buffer readings using the processor module.
  3. Periodically aggregate buffered data and dispatch processed
     payloads to the cloud backend.

Run:
    python fog_node.py
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
import yaml
from flask import Flask, jsonify, request

from processor import BufferRegistry

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FOG NODE] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Load configuration ─────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "fog_config.yaml")
    with open(config_path, "r") as fh:
        return yaml.safe_load(fh)


cfg = load_config()

# Allow overriding the backend URL via environment variable (for Docker / AWS)
BACKEND_URL      = os.getenv("BACKEND_URL",      cfg["backend_url"])
DISPATCH_INTERVAL = int(os.getenv("DISPATCH_INTERVAL", cfg["dispatch_interval"]))
FOG_NODE_ID      = os.getenv("FOG_NODE_ID",      cfg["fog_node_id"])

# ── Buffer registry (shared state, protected by a lock) ────────────────────────
_lock    = threading.Lock()
_registry = BufferRegistry(
    window_size = cfg["rolling_window_size"],
    z_threshold = cfg["anomaly_z_threshold"],
)

# Simple stats counter
_stats = {"received": 0, "dispatched": 0, "errors": 0}

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/sensor-data", methods=["POST"])
def receive_sensor_data():
    """
    Endpoint called by each sensor simulator.
    Validates the payload, then adds it to the buffer registry.
    """
    data = request.get_json(silent=True)

    # ── Validation ─────────────────────────────────────────────────────────────
    required = {"sensor_id", "sensor_type", "value", "unit", "timestamp"}
    if not data or not required.issubset(data.keys()):
        missing = required - set(data.keys() if data else [])
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    if not isinstance(data["value"], (int, float)):
        return jsonify({"error": "value must be numeric"}), 400

    # ── Buffer ─────────────────────────────────────────────────────────────────
    with _lock:
        _registry.record(data)
        _stats["received"] += 1

    log.debug("Buffered %s = %.3f %s", data["sensor_id"], data["value"], data["unit"])
    return jsonify({"status": "buffered"}), 200


@app.route("/health", methods=["GET"])
def health():
    """Simple health check endpoint."""
    return jsonify({
        "status":      "ok",
        "fog_node_id": FOG_NODE_ID,
        "stats":       _stats,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    })


@app.route("/stats", methods=["GET"])
def stats():
    """Return current fog node statistics."""
    with _lock:
        return jsonify({
            "fog_node_id":    FOG_NODE_ID,
            "sensors_tracked": _registry.sensor_count,
            "stats":          _stats,
        })


# ── Background dispatcher ──────────────────────────────────────────────────────

def dispatch_loop():
    """
    Runs in a background thread.
    Every DISPATCH_INTERVAL seconds:
      1. Flushes all sensor buffers to get processed readings.
      2. POSTs the aggregated payload to the cloud backend.
    """
    log.info("Dispatcher started — flushing to backend every %ds", DISPATCH_INTERVAL)
    while True:
        time.sleep(DISPATCH_INTERVAL)
        _dispatch()


def _dispatch():
    """Flush buffers and send one batch to the backend."""
    with _lock:
        readings = _registry.flush_all()

    if not readings:
        log.debug("Nothing to dispatch (buffers empty).")
        return

    payload = {
        "fog_node_id": FOG_NODE_ID,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "readings":    readings,
    }

    log.info("Dispatching %d processed reading(s) to backend…", len(readings))

    for attempt in range(3):
        try:
            resp = requests.post(
                BACKEND_URL,
                json=payload,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                _stats["dispatched"] += len(readings)
                log.info("Backend accepted %d reading(s). (total dispatched: %d)",
                         len(readings), _stats["dispatched"])
                return
            log.warning("Backend returned %d — attempt %d",
                        resp.status_code, attempt + 1)
        except requests.exceptions.ConnectionError:
            log.warning("Backend unreachable (attempt %d/3) — "
                        "is it running at %s?", attempt + 1, BACKEND_URL)
            time.sleep(2 ** attempt)   # exponential back-off: 1s, 2s, 4s
        except Exception as exc:
            log.error("Dispatch error: %s", exc)
            _stats["errors"] += 1
            return

    log.error("All 3 dispatch attempts failed. Readings dropped.")
    _stats["errors"] += 1


# ── Startup ────────────────────────────────────────────────────────────────────

def main():
    # Start background dispatcher thread
    dispatcher = threading.Thread(target=dispatch_loop, daemon=True)
    dispatcher.start()

    host = cfg.get("host", "0.0.0.0")
    port = int(os.getenv("FOG_PORT", cfg.get("port", 5001)))

    log.info("Fog node %s listening on %s:%d", FOG_NODE_ID, host, port)
    log.info("Will dispatch to: %s", BACKEND_URL)

    # Disable Flask's default reloader so the dispatcher thread is not duplicated
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
