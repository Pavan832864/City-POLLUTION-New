// ─────────────────────────────────────────────────────────────────────────────
// Dashboard Configuration
// Smart City Pollution Monitoring System
//
// Update API_BASE_URL to your API Gateway invoke URL after deploying to AWS.
// Example: "https://abc123xyz.execute-api.eu-west-1.amazonaws.com/prod"
//
// This file is loaded separately from index.html so you only need to update
// it once when your API Gateway URL changes — not on every dashboard deploy.
// ─────────────────────────────────────────────────────────────────────────────

const CONFIG = {
  API_BASE_URL:    "https://zhgqcm54si.execute-api.us-east-1.amazonaws.com/prod",
  POLL_INTERVAL_MS: 10000,   // how often the dashboard refreshes (ms)
};
