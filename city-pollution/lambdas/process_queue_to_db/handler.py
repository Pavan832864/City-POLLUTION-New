"""
Smart City Pollution Monitoring System
Lambda: ProcessQueueToDB

Architecture position:
    SQS Queue  →  [THIS LAMBDA]  →  DynamoDB

Responsibilities:
  1. Consume SQS messages (batches of fog-node sensor readings).
  2. Parse and transform each reading into a DynamoDB item.
  3. Persist items using DynamoDB batch_writer for efficiency.
  4. Report partial failures back to SQS so failed messages are retried.

SQS trigger configuration (set in AWS console):
  - Batch size          : 10  (up to 10 messages per Lambda invocation)
  - Report batch item failures: enabled  (allows per-message retry)

DynamoDB table schema:
  Table name   : PollutionReadings  (set via DYNAMODB_TABLE env var)
  Partition key: sensor_id   (String)
  Sort key     : sensor_ts   (String — ISO 8601 UTC timestamp)
  GSI          : sensor_type-sensor_ts-index
                   PK = sensor_type (String)
                   SK = sensor_ts   (String)
                 (create this GSI in the console for efficient type queries)

Environment variables:
  DYNAMODB_TABLE  — Name of the DynamoDB table (e.g. PollutionReadings)
"""

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS resources ──────────────────────────────────────────────────────────────
dynamodb   = boto3.resource("dynamodb")
TABLE_NAME = os.environ["DYNAMODB_TABLE"]
table      = dynamodb.Table(TABLE_NAME)


# ── Handler ────────────────────────────────────────────────────────────────────

def handler(event, context):
    """
    Lambda entrypoint — called by SQS with a batch of messages.
    Returns batchItemFailures so SQS can selectively retry failed messages.
    """
    total_saved  = 0
    failed_items = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            body = json.loads(record["body"])
            count = _process_fog_batch(body)
            total_saved += count
            logger.info(
                "Saved %d reading(s) from message %s (fog node: %s)",
                count, message_id, body.get("fog_node_id", "unknown"),
            )
        except Exception as exc:
            logger.error(
                "Failed to process SQS message %s: %s", message_id, exc
            )
            # Tell SQS to re-deliver this specific message
            failed_items.append({"itemIdentifier": message_id})

    logger.info(
        "Batch complete — %d reading(s) saved, %d message(s) failed",
        total_saved, len(failed_items),
    )

    if failed_items:
        return {"batchItemFailures": failed_items}
    return {}


# ── Batch processing ───────────────────────────────────────────────────────────

def _process_fog_batch(body: dict) -> int:
    """
    Write all readings in a fog-node batch to DynamoDB.
    Returns the number of items written.
    """
    fog_node_id = body.get("fog_node_id", "unknown")
    ingested_at = datetime.now(timezone.utc).isoformat()
    readings    = body.get("readings", [])

    if not readings:
        return 0

    with table.batch_writer() as batch:
        for reading in readings:
            item = _build_dynamo_item(reading, fog_node_id, ingested_at)
            batch.put_item(Item=item)

    return len(readings)


# ── Item builder ───────────────────────────────────────────────────────────────

def _build_dynamo_item(r: dict, fog_node_id: str, ingested_at: str) -> dict:
    """
    Map a fog-node reading dict to a DynamoDB attribute map.

    DynamoDB does not accept Python float — all numbers must be Decimal.
    None values are dropped (DynamoDB rejects null attributes).
    """
    agg = r.get("aggregates") or {}
    cat = r.get("aqi_category") or {}

    item = {
        # ── Primary key ──────────────────────────────────────────────────────
        "sensor_id":   r.get("sensor_id", "unknown"),
        "sensor_ts":   r.get("timestamp", ingested_at),

        # ── Metadata ─────────────────────────────────────────────────────────
        "fog_node_id": fog_node_id,
        "sensor_type": r.get("sensor_type", "unknown"),
        "location":    r.get("location", "unknown"),
        "unit":        r.get("unit", ""),

        # ── Sensor reading ───────────────────────────────────────────────────
        "raw_value": _dec(r.get("raw_value")),
        "quality":   r.get("quality", "unknown"),

        # ── Rolling aggregates (computed by fog node) ─────────────────────────
        "agg_mean":  _dec(agg.get("mean")),
        "agg_min":   _dec(agg.get("min")),
        "agg_max":   _dec(agg.get("max")),
        "agg_std":   _dec(agg.get("std")),
        "agg_count": agg.get("count"),

        # ── AQI ───────────────────────────────────────────────────────────────
        "aqi":        r.get("aqi"),
        "aqi_label":  cat.get("label"),
        "aqi_colour": cat.get("colour"),

        # ── Anomaly flag ──────────────────────────────────────────────────────
        "anomaly": bool(r.get("anomaly", False)),

        # ── Timestamps ────────────────────────────────────────────────────────
        "ingested_at": ingested_at,
    }

    # Drop None / empty-string values — DynamoDB rejects them
    return {k: v for k, v in item.items() if v is not None and v != ""}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dec(value) -> Decimal | None:
    """Safely convert a numeric value to Decimal for DynamoDB storage."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
