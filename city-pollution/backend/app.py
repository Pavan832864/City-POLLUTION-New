"""
Smart City Pollution Monitoring System
Backend – Layer 3 (Scalable Cloud Backend)

Endpoints:
  POST /ingest          — Receive processed payload from fog node
  GET  /readings        — Query stored readings (filterable)
  GET  /stats           — Summary stats per sensor type
  GET  /stream          — Server-Sent Events stream (real-time push)
  GET  /health          — Health check
  GET  /                — Serve the HTML dashboard

Scalability patterns used:
  - SSE for real-time push (no polling)
  - SQLAlchemy ORM (swap SQLite → RDS/DynamoDB via DATABASE_URL)
  - Stateless HTTP design (ready for horizontal scaling behind a load balancer)
  - In-memory pub/sub for SSE fan-out (swap for Redis pub/sub in production)

Run:
    python app.py
"""

import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request
from sqlalchemy.orm import Session

from database import SessionLocal, init_db
from models import SensorReading

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BACKEND] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")

# ── SSE pub/sub (in-memory, replace with Redis pub/sub for multi-instance) ─────
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def _subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=50)
    with _sub_lock:
        _subscribers.append(q)
    return q


def _unsubscribe(q: queue.Queue):
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def _publish(event_data: dict):
    """Fan-out an event to all connected SSE clients."""
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event_data)
            except queue.Full:
                dead.append(q)   # slow / disconnected client
        for q in dead:
            _subscribers.remove(q)


# ── Stats counters ─────────────────────────────────────────────────────────────
_stats = {"ingested": 0, "anomalies": 0, "sse_clients": 0}


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the real-time dashboard."""
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({
        "status":      "ok",
        "stats":       _stats,
        "sse_clients": len(_subscribers),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    })


@app.route("/ingest", methods=["POST"])
def ingest():
    """
    Receive a processed batch from the fog node.
    Persists each reading to the database and broadcasts via SSE.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    fog_node_id = data.get("fog_node_id", "unknown")
    readings    = data.get("readings", [])

    if not isinstance(readings, list) or not readings:
        return jsonify({"error": "readings must be a non-empty list"}), 400

    saved = []
    db: Session = SessionLocal()
    try:
        for r in readings:
            # Parse the sensor timestamp (ISO 8601)
            try:
                ts = datetime.fromisoformat(r.get("timestamp", "").replace("Z", "+00:00"))
                ts = ts.replace(tzinfo=None)   # store as naive UTC in SQLite
            except (ValueError, AttributeError):
                ts = datetime.utcnow()

            agg  = r.get("aggregates", {})
            aqi  = r.get("aqi")
            cat  = r.get("aqi_category") or {}

            reading = SensorReading(
                sensor_id   = r.get("sensor_id", "unknown"),
                sensor_type = r.get("sensor_type", "unknown"),
                location    = r.get("location", "unknown"),
                unit        = r.get("unit", ""),
                fog_node_id = fog_node_id,
                raw_value   = float(r.get("raw_value", 0)),
                quality     = r.get("quality"),
                agg_mean    = agg.get("mean"),
                agg_min     = agg.get("min"),
                agg_max     = agg.get("max"),
                agg_std     = agg.get("std"),
                agg_count   = agg.get("count"),
                aqi         = aqi,
                aqi_label   = cat.get("label"),
                aqi_colour  = cat.get("colour"),
                anomaly     = bool(r.get("anomaly", False)),
                sensor_ts   = ts,
            )
            db.add(reading)
            saved.append(reading)

            if reading.anomaly:
                _stats["anomalies"] += 1
                log.warning("ANOMALY detected: %s = %.3f %s (AQI %s)",
                            reading.sensor_id, reading.raw_value,
                            reading.unit, reading.aqi)

        db.commit()

        # Refresh to get auto-generated IDs before closing session
        for row in saved:
            db.refresh(row)

        saved_dicts = [row.to_dict() for row in saved]

    finally:
        db.close()

    _stats["ingested"] += len(saved_dicts)
    log.info("Ingested %d reading(s) from %s (total: %d)",
             len(saved_dicts), fog_node_id, _stats["ingested"])

    # Broadcast to all SSE clients
    _publish({"type": "readings", "data": saved_dicts})

    return jsonify({"status": "ok", "saved": len(saved_dicts)}), 201


@app.route("/readings")
def get_readings():
    """
    Return stored readings.
    Query params:
      - sensor_type : filter by type (e.g. PM2.5)
      - sensor_id   : filter by specific sensor
      - limit       : max rows (default 100, max 500)
      - anomaly     : "true" to return only anomalies
    """
    sensor_type = request.args.get("sensor_type")
    sensor_id   = request.args.get("sensor_id")
    anomaly     = request.args.get("anomaly", "").lower() == "true"
    limit       = min(int(request.args.get("limit", 100)), 500)

    db: Session = SessionLocal()
    try:
        q = db.query(SensorReading).order_by(SensorReading.sensor_ts.desc())
        if sensor_type:
            q = q.filter(SensorReading.sensor_type == sensor_type)
        if sensor_id:
            q = q.filter(SensorReading.sensor_id == sensor_id)
        if anomaly:
            q = q.filter(SensorReading.anomaly == True)
        rows = q.limit(limit).all()
        return jsonify([r.to_dict() for r in rows])
    finally:
        db.close()


@app.route("/stats")
def get_stats():
    """
    Return the latest reading and aggregate stats for each sensor type.
    """
    db: Session = SessionLocal()
    try:
        # Distinct sensor types present in DB
        types = [row[0] for row in db.query(SensorReading.sensor_type).distinct()]

        result = {}
        for st in types:
            rows = (db.query(SensorReading)
                    .filter(SensorReading.sensor_type == st)
                    .order_by(SensorReading.sensor_ts.desc())
                    .limit(50)
                    .all())
            if not rows:
                continue
            latest = rows[0]
            values = [r.raw_value for r in rows if r.raw_value is not None]
            result[st] = {
                "latest_value":  latest.raw_value,
                "latest_aqi":    latest.aqi,
                "aqi_label":     latest.aqi_label,
                "aqi_colour":    latest.aqi_colour,
                "location":      latest.location,
                "unit":          latest.unit,
                "total_readings": len(values),
                "anomaly_count": sum(1 for r in rows if r.anomaly),
            }
        return jsonify(result)
    finally:
        db.close()


@app.route("/stream")
def stream():
    """
    Server-Sent Events endpoint.
    Dashboard clients connect here and receive live reading updates.
    """
    _stats["sse_clients"] = len(_subscribers) + 1

    def event_generator():
        q = _subscribe()
        log.info("SSE client connected (total: %d)", len(_subscribers))
        try:
            # Send a connection confirmation
            yield "data: {\"type\": \"connected\"}\n\n"
            while True:
                try:
                    event = q.get(timeout=20)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Send keepalive comment to prevent proxy timeouts
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            _unsubscribe(q)
            log.info("SSE client disconnected (total: %d)", len(_subscribers))

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    log.info("Database initialised.")

    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    port = int(os.getenv("BACKEND_PORT", 5002))

    log.info("Backend running on http://%s:%d", host, port)
    log.info("Dashboard: http://localhost:%d/", port)

    # threaded=True allows concurrent SSE + ingest connections
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
