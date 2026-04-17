============================================================
 Smart City Pollution Monitoring System
 NCI MSc Cloud Computing 2026 — Fog & Edge Computing (H9FECC)
 Student: Bishwajit Banik  |  ID: x23456789
============================================================

OVERVIEW
--------
A three-layer IoT architecture for real-time urban air-quality monitoring:

  Layer 1 – Sensors   : Simulate 5 EPA-standard pollution sensor types
                         (PM2.5, PM10, NO2, CO, O3) with individually
                         configurable dispatch intervals, base values,
                         noise factors, and spike probabilities.

  Layer 2 – Fog Node  : Validates and buffers incoming sensor readings,
                         computes rolling aggregates (mean/min/max/std),
                         calculates US-EPA AQI, detects anomalies via
                         Z-score, then dispatches batches to the cloud.

  Layer 3 – Cloud     : Serverless AWS pipeline:
                           API Gateway  →  IngestToQueue (Lambda)
                           →  SQS Queue  →  ProcessQueueToDB (Lambda)
                           →  DynamoDB
                           S3 Dashboard  →  API Gateway
                           →  GetDashboardData (Lambda)  →  DynamoDB


PROJECT STRUCTURE
-----------------
city-pollution/
├── sensors/
│   ├── sensor_simulator.py        # 5 sensor threads, configurable rates
│   └── sensor_config.yaml         # sensor IDs, locations, dispatch intervals
│
├── fog_node/
│   ├── fog_node.py                # Flask receiver + background dispatcher
│   ├── processor.py               # AQI calc, aggregation, Z-score anomaly
│   └── fog_config.yaml            # dispatch interval, backend URL, thresholds
│
├── lambdas/                       # AWS Lambda function code
│   ├── ingest_to_queue/
│   │   ├── handler.py             # Receives POST from API Gateway → SQS
│   │   └── requirements.txt
│   ├── process_queue_to_db/
│   │   ├── handler.py             # SQS trigger → writes to DynamoDB
│   │   └── requirements.txt
│   └── get_dashboard_data/
│       ├── handler.py             # GET /readings + /stats from DynamoDB
│       └── requirements.txt
│
├── dashboard/
│   ├── index.html                 # Static S3-hosted Chart.js dashboard
│   └── config.js                  # API Gateway URL config (edit before upload)
│
├── backend/                       # Local development Flask backend (reference)
│   ├── app.py                     # Flask API with SSE + SQLite
│   ├── models.py                  # SQLAlchemy ORM model
│   ├── database.py                # DB engine setup
│   └── templates/index.html       # Local SSE dashboard
│
├── docker-compose.yml             # Full-stack local orchestration
├── Dockerfile.sensors
├── Dockerfile.fog
├── Dockerfile.backend
├── requirements.txt               # Python dependencies (local dev)
└── README.txt


PREREQUISITES
-------------
  Python 3.10+  OR  Docker Desktop (for local development)
  AWS account   (for cloud deployment)


==============================================================
 OPTION A — Run Locally with Docker Compose (recommended)
==============================================================
Uses the local Flask backend (backend/app.py) with SQLite.

  cd city-pollution
  docker compose up --build

  Dashboard:   http://localhost:5002
  Fog health:  http://localhost:5001/health
  Fog stats:   http://localhost:5001/stats

Stop all services:
  docker compose down


==============================================================
 OPTION B — Run Locally without Docker (3 terminals)
==============================================================
Terminal 1 – Start the backend first:
  pip install -r requirements.txt
  cd city-pollution
  python backend/app.py
  → API + Dashboard: http://localhost:5002

Terminal 2 – Start the fog node:
  cd city-pollution
  python fog_node/fog_node.py

Terminal 3 – Start the sensor simulators:
  cd city-pollution
  python sensors/sensor_simulator.py

Open http://localhost:5002 to see the live dashboard.


==============================================================
 OPTION C — AWS Serverless Deployment
==============================================================
Full step-by-step instructions are in AWS_Deployment_Guide.docx.
Brief summary:

Prerequisites:
  - AWS account with IAM permissions
  - AWS CLI configured (aws configure)

Step 1 — IAM Role
  Create role "PollutionLambdaRole" with:
    AWSLambdaBasicExecutionRole
    AmazonDynamoDBFullAccess
    AmazonSQSFullAccess

Step 2 — DynamoDB
  Table name : PollutionReadings
  Partition key : sensor_id (String)
  Sort key      : sensor_ts (String)
  Capacity mode : On-demand
  GSI (optional): sensor_type-sensor_ts-index

Step 3 — SQS Queue
  Name             : PollutionQueue
  Type             : Standard
  Visibility timeout: 60 seconds

Step 4 — Lambda Functions (deploy each from lambdas/ folder)
  a) IngestToQueue
     zip ingest_to_queue.zip handler.py
     Runtime: Python 3.12 | Handler: handler.handler | Timeout: 30s
     Env var: SQS_QUEUE_URL = <your PollutionQueue URL>

  b) ProcessQueueToDB
     zip process_queue_to_db.zip handler.py
     Runtime: Python 3.12 | Handler: handler.handler | Timeout: 60s
     Env var: DYNAMODB_TABLE = PollutionReadings
     SQS trigger: PollutionQueue (batch size 10, report batch failures ON)

  c) GetDashboardData
     zip get_dashboard_data.zip handler.py
     Runtime: Python 3.12 | Handler: handler.handler | Timeout: 30s
     Env var: DYNAMODB_TABLE = PollutionReadings

Step 5 — API Gateway
  REST API name: PollutionAPI
  Routes (Lambda Proxy Integration, CORS enabled):
    POST /ingest  → IngestToQueue
    GET  /readings → GetDashboardData
    GET  /stats    → GetDashboardData
  Deploy to stage: prod
  Note the Invoke URL (e.g. https://<id>.execute-api.<region>.amazonaws.com/prod)

Step 6 — S3 Dashboard
  a) Edit dashboard/config.js — set API_BASE_URL to your API Gateway Invoke URL
     (no trailing slash)
  b) Create S3 bucket, enable static website hosting (index.html)
  c) Set public read bucket policy
  d) Upload dashboard/index.html and dashboard/config.js to bucket root

Step 7 — Configure Fog Node
  Edit fog_node/fog_config.yaml:
    backend_url: "https://<api-id>.execute-api.<region>.amazonaws.com/prod/ingest"
  Or set environment variable BACKEND_URL when running via Docker.


==============================================================
 CONFIGURATION REFERENCE
==============================================================
sensors/sensor_config.yaml
  - dispatch_interval  : seconds between sensor readings (per sensor)
  - base_value         : typical background concentration
  - noise_factor       : Gaussian noise amplitude (fraction of base_value)
  - spike_probability  : probability of injecting an anomaly spike (0.0–1.0)

fog_node/fog_config.yaml
  - backend_url        : HTTP endpoint to dispatch batches to
  - dispatch_interval  : how often (seconds) the fog node flushes to backend
  - anomaly_z_threshold: Z-score threshold for anomaly detection (default 2.5)
  - rolling_window_size: number of readings per sensor kept in rolling buffer

Environment variables (override YAML — useful for Docker / AWS):
  FOG_NODE_URL       URL sensors POST to  (default: http://localhost:5001/sensor-data)
  BACKEND_URL        URL fog node POSTs to (default: http://localhost:5002/ingest)
  DISPATCH_INTERVAL  Fog dispatch period in seconds (default: 15)
  DATABASE_URL       SQLAlchemy DB URL for local backend (default: sqlite:///./pollution.db)
  FOG_NODE_ID        Fog node identifier (default: FOG-DUBLIN-001)
  FOG_PORT           Fog node HTTP port (default: 5001)
  SQS_QUEUE_URL      (Lambda only) Full SQS queue URL
  DYNAMODB_TABLE     (Lambda only) DynamoDB table name


==============================================================
 LOCAL API ENDPOINTS (Flask backend)
==============================================================
  GET  /              Real-time dashboard (HTML + Chart.js)
  GET  /health        Health check (JSON)
  POST /ingest        Receive fog node payload
  GET  /readings      Query stored readings
                        ?sensor_type=PM2.5
                        ?sensor_id=PM25-DUBLIN-001
                        ?anomaly=true
                        ?limit=100
  GET  /stats         Latest reading + AQI summary per sensor type
  GET  /stream        SSE stream (real-time push to dashboard)
  GET  /health        Fog node health + dispatch stats (port 5001)

AWS API Gateway endpoints (after deployment):
  POST <invoke-url>/ingest     Fog node dispatch
  GET  <invoke-url>/readings   Dashboard data query
  GET  <invoke-url>/stats      Dashboard stats query


==============================================================
 SENSOR TYPES & US-EPA AQI THRESHOLDS
==============================================================
  Sensor  Unit   Good (0–50)  Moderate (51–100)  Unhealthy (101–150)
  PM2.5   µg/m³  0–12.0       12.1–35.4          35.5–55.4
  PM10    µg/m³  0–54         55–154             155–254
  NO2     ppb    0–53         54–100             101–360
  CO      ppm    0–4.4        4.5–9.4            9.5–12.4
  O3      ppb    0–54         55–70              71–85


ACADEMIC INTEGRITY
------------------
This work is submitted in fulfilment of the Fog and Edge Computing (H9FECC)
continuous assessment at National College of Ireland, MSc Cloud Computing 2026.
All code is original work by Bishwajit Banik (x23456789) unless explicitly cited.
