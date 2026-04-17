"""
Smart City Pollution Monitoring System
Lambda: IngestToQueue

Architecture position:
    Fog Node  →  API Gateway  →  [THIS LAMBDA]  →  SQS Queue

Responsibilities:
  1. Receive the processed batch payload from the Fog Node via HTTP POST.
  2. Validate required fields.
  3. Forward the entire batch as a single SQS message for async persistence.

API Gateway integration:
  - Method  : POST
  - Path    : /ingest
  - Proxy   : Lambda Proxy Integration (passes full event)

Environment variables:
  SQS_QUEUE_URL  — Full URL of the SQS processing queue
                   e.g. https://sqs.eu-west-1.amazonaws.com/123456789/PollutionQueue
"""

import json
import logging
import os

import boto3

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients ────────────────────────────────────────────────────────────────
sqs = boto3.client("sqs")
SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]

# ── CORS headers (required for S3-hosted dashboard) ───────────────────────────
CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type":                 "application/json",
}


# ── Handler ────────────────────────────────────────────────────────────────────

def handler(event, context):
    """
    Lambda entrypoint.
    Called by API Gateway with a Lambda Proxy Integration event.
    """
    # Handle CORS preflight from the browser dashboard
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    # ── Parse body ─────────────────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _error(400, "Request body is not valid JSON.")

    # ── Validate ───────────────────────────────────────────────────────────────
    fog_node_id = body.get("fog_node_id")
    readings    = body.get("readings")

    if not fog_node_id:
        return _error(400, "Missing required field: fog_node_id")

    if not isinstance(readings, list) or len(readings) == 0:
        return _error(400, "readings must be a non-empty list")

    # ── Enqueue ────────────────────────────────────────────────────────────────
    # The entire fog-node batch is sent as one SQS message.
    # ProcessQueueToDB will fan out individual readings to DynamoDB.
    try:
        response = sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(body),
            MessageAttributes={
                "fog_node_id": {
                    "StringValue": fog_node_id,
                    "DataType":    "String",
                },
                "reading_count": {
                    "StringValue": str(len(readings)),
                    "DataType":    "String",
                },
            },
        )
    except Exception as exc:
        logger.error("Failed to enqueue message from %s: %s", fog_node_id, exc)
        return _error(500, "Failed to enqueue message. Please retry.")

    message_id = response["MessageId"]
    logger.info(
        "Queued %d reading(s) from fog node '%s' — SQS MessageId: %s",
        len(readings), fog_node_id, message_id,
    )

    return {
        "statusCode": 202,
        "headers":    CORS_HEADERS,
        "body": json.dumps({
            "status":        "queued",
            "message_id":    message_id,
            "reading_count": len(readings),
        }),
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _error(status_code: int, message: str) -> dict:
    logger.warning("Returning %d: %s", status_code, message)
    return {
        "statusCode": status_code,
        "headers":    CORS_HEADERS,
        "body":       json.dumps({"error": message}),
    }
