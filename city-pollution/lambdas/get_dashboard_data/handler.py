"""
Smart City Pollution Monitoring System
Lambda: GetDashboardData

Architecture position:
    S3 Dashboard  →  API Gateway  →  [THIS LAMBDA]  →  DynamoDB

Responsibilities:
  1. Serve sensor reading data to the S3-hosted static dashboard.
  2. Handle two routes (configured in API Gateway):
       GET /readings  — returns recent sensor readings (filterable)
       GET /stats     — returns latest value + summary per sensor type

API Gateway integration:
  - Method  : GET
  - Paths   : /readings   and   /stats
  - Proxy   : Lambda Proxy Integration
  - CORS    : enabled (S3 dashboard origin)

Query string parameters for /readings:
  sensor_type  — filter by type       (e.g. PM2.5)
  sensor_id    — filter by sensor ID
  anomaly      — "true" to return only anomalies
  limit        — max items to return  (default 100, max 500)

DynamoDB scan strategy:
  - For MVP a full table Scan with filter expressions is used.
  - For production, add the recommended GSI and switch to Query:
      GSI: sensor_type-sensor_ts-index  (PK=sensor_type, SK=sensor_ts)

Environment variables:
  DYNAMODB_TABLE  — Name of the DynamoDB table (e.g. PollutionReadings)
"""

import json
import logging
import os
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS resources ──────────────────────────────────────────────────────────────
dynamodb   = boto3.resource("dynamodb")
TABLE_NAME = os.environ["DYNAMODB_TABLE"]
table      = dynamodb.Table(TABLE_NAME)

# ── CORS headers ───────────────────────────────────────────────────────────────
CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Content-Type":                 "application/json",
}


# ── Handler ────────────────────────────────────────────────────────────────────

def handler(event, context):
    """Lambda entrypoint — routes to the correct sub-handler by path."""
    method = event.get("httpMethod", "GET")
    path   = event.get("path", "/")
    params = event.get("queryStringParameters") or {}

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    if path.endswith("/readings"):
        return _get_readings(params)
    elif path.endswith("/stats"):
        return _get_stats()
    else:
        return _response(404, {"error": f"No route for path: {path}"})


# ── /readings ──────────────────────────────────────────────────────────────────

def _get_readings(params: dict) -> dict:
    """
    Return recent sensor readings.
    Applies optional filters for sensor_type, sensor_id, and anomaly flag.
    """
    sensor_type  = params.get("sensor_type")
    sensor_id    = params.get("sensor_id")
    only_anomaly = params.get("anomaly", "").lower() == "true"
    limit        = min(int(params.get("limit", 100)), 500)

    # Build filter expression
    filter_expr = None
    if sensor_type:
        f = Attr("sensor_type").eq(sensor_type)
        filter_expr = f if filter_expr is None else filter_expr & f
    if sensor_id:
        f = Attr("sensor_id").eq(sensor_id)
        filter_expr = f if filter_expr is None else filter_expr & f
    if only_anomaly:
        f = Attr("anomaly").eq(True)
        filter_expr = f if filter_expr is None else filter_expr & f

    scan_kwargs = {}
    if filter_expr:
        scan_kwargs["FilterExpression"] = filter_expr

    # Scan — paginate until we have enough items
    items = []
    response = table.scan(**scan_kwargs)
    items.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response and len(items) < limit:
        response = table.scan(
            **scan_kwargs,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    # Sort newest-first, then cap at limit
    items.sort(key=lambda x: x.get("sensor_ts", ""), reverse=True)
    items = items[:limit]

    logger.info(
        "GET /readings — filters=%s anomaly=%s → %d item(s)",
        {"sensor_type": sensor_type, "sensor_id": sensor_id},
        only_anomaly, len(items),
    )

    return _response(200, [_format_reading(item) for item in items])


# ── /stats ─────────────────────────────────────────────────────────────────────

def _get_stats() -> dict:
    """
    Return the latest reading and aggregate statistics for each sensor type.
    """
    # Full scan — acceptable for the small dataset in this project
    items = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    # Group by sensor_type
    by_type: dict[str, list] = {}
    for item in items:
        st = item.get("sensor_type", "unknown")
        by_type.setdefault(st, []).append(item)

    result = {}
    for st, rows in by_type.items():
        rows.sort(key=lambda x: x.get("sensor_ts", ""), reverse=True)
        latest = rows[0]
        result[st] = {
            "latest_value":   _float(latest.get("raw_value")),
            "latest_aqi":     latest.get("aqi"),
            "aqi_label":      latest.get("aqi_label"),
            "aqi_colour":     latest.get("aqi_colour"),
            "location":       latest.get("location"),
            "unit":           latest.get("unit"),
            "total_readings": len(rows),
            "anomaly_count":  sum(1 for r in rows if r.get("anomaly")),
        }

    logger.info("GET /stats — %d sensor type(s) returned", len(result))
    return _response(200, result)


# ── Serialisation helpers ──────────────────────────────────────────────────────

def _format_reading(item: dict) -> dict:
    """
    Shape a raw DynamoDB item into the response format expected by the dashboard.
    Matches the structure previously returned by the Flask backend's to_dict().
    """
    return {
        "sensor_id":   item.get("sensor_id"),
        "sensor_type": item.get("sensor_type"),
        "location":    item.get("location"),
        "unit":        item.get("unit"),
        "fog_node_id": item.get("fog_node_id"),
        "raw_value":   _float(item.get("raw_value")),
        "quality":     item.get("quality"),
        "aggregates": {
            "mean":  _float(item.get("agg_mean")),
            "min":   _float(item.get("agg_min")),
            "max":   _float(item.get("agg_max")),
            "std":   _float(item.get("agg_std")),
            "count": item.get("agg_count"),
        },
        "aqi": {
            "value":  item.get("aqi"),
            "label":  item.get("aqi_label"),
            "colour": item.get("aqi_colour"),
        },
        "anomaly":     item.get("anomaly", False),
        "sensor_ts":   item.get("sensor_ts"),
        "ingested_at": item.get("ingested_at"),
    }


def _float(val) -> float | None:
    """Safely convert Decimal or numeric to float."""
    if val is None:
        return None
    return float(val)


def _serialise(obj):
    """Recursively convert Decimal → float/int for JSON serialisation."""
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialise(v) for v in obj]
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def _response(status_code: int, body) -> dict:
    return {
        "statusCode": status_code,
        "headers":    CORS_HEADERS,
        "body":       json.dumps(_serialise(body)),
    }
