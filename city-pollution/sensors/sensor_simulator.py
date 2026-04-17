"""
Smart City Pollution Monitoring System
Sensor Simulator - Layer 1

Simulates 5 pollution sensor types:
  - PM2.5  (Fine particulate matter, µg/m³)
  - PM10   (Coarse particulate matter, µg/m³)
  - NO2    (Nitrogen dioxide, ppb)
  - CO     (Carbon monoxide, ppm)
  - O3     (Ozone, ppb)

Each sensor runs in its own thread with a configurable dispatch interval
and sends readings to the fog node via HTTP POST.
"""

import os
import json
import math
import random
import threading
import time
import logging
from datetime import datetime, timezone

import requests
import yaml

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SENSOR] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Sensor value bounds (for realistic clamping) ───────────────────────────────
SENSOR_BOUNDS = {
    "PM2.5": (0.0, 500.0),
    "PM10":  (0.0, 600.0),
    "NO2":   (0.0, 2000.0),
    "CO":    (0.0, 50.0),
    "O3":    (0.0, 200.0),
}

# ── AQI colour categories (for log display) ────────────────────────────────────
def quality_label(sensor_type: str, value: float) -> str:
    """Return a human-readable quality label based on EPA thresholds."""
    thresholds = {
        "PM2.5": [(12.0, "Good"), (35.4, "Moderate"), (55.4, "Unhealthy (SG)"), (150.4, "Unhealthy")],
        "PM10":  [(54.0, "Good"), (154.0, "Moderate"), (254.0, "Unhealthy (SG)"), (354.0, "Unhealthy")],
        "NO2":   [(53.0, "Good"), (100.0, "Moderate"), (360.0, "Unhealthy (SG)"), (649.0, "Unhealthy")],
        "CO":    [(4.4,  "Good"), (9.4,   "Moderate"), (12.4,  "Unhealthy (SG)"), (15.4,  "Unhealthy")],
        "O3":    [(54.0, "Good"), (70.0,  "Moderate"), (85.0,  "Unhealthy (SG)"), (105.0, "Unhealthy")],
    }
    for threshold, label in thresholds.get(sensor_type, []):
        if value <= threshold:
            return label
    return "Hazardous"


class PollutionSensor:
    """
    A virtual pollution sensor that generates realistic readings using:
      - A base value (typical background concentration)
      - Time-of-day traffic pattern (rush-hour peaks at 08:00 and 17:30)
      - Gaussian random noise
      - Occasional anomaly spikes to test fog-node detection
    """

    def __init__(self, config: dict, fog_node_url: str):
        self.sensor_id       = config["id"]
        self.sensor_type     = config["type"]
        self.location        = config["location"]
        self.unit            = config["unit"]
        self.dispatch_interval = config["dispatch_interval"]
        self.base_value      = config["base_value"]
        self.noise_factor    = config["noise_factor"]
        self.spike_prob      = config.get("spike_probability", 0.02)
        self.fog_node_url    = fog_node_url
        self._stop_event     = threading.Event()
        self._thread         = threading.Thread(target=self._run, daemon=True)

    # ── Public interface ───────────────────────────────────────────────────────

    def start(self):
        log.info("Starting sensor %s (%s) @ %ss interval",
                 self.sensor_id, self.sensor_type, self.dispatch_interval)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def join(self):
        self._thread.join()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _time_of_day_factor(self) -> float:
        """
        Returns a multiplier (1.0–1.8) that peaks during morning and
        evening rush hours to mimic urban traffic pollution patterns.
        """
        hour = datetime.now().hour + datetime.now().minute / 60.0
        # Two Gaussian bumps: morning rush ~08:00, evening rush ~17:30
        morning = math.exp(-0.5 * ((hour - 8.0) / 1.5) ** 2)
        evening = math.exp(-0.5 * ((hour - 17.5) / 1.5) ** 2)
        return 1.0 + 0.8 * max(morning, evening)

    def _generate_reading(self) -> float:
        """Generate a single sensor reading."""
        tod_factor = self._time_of_day_factor()
        base = self.base_value * tod_factor

        # Gaussian noise around base
        noise = random.gauss(0, base * self.noise_factor)
        value = base + noise

        # Occasional spike (anomaly injection)
        if random.random() < self.spike_prob:
            spike = base * random.uniform(2.5, 4.0)
            value += spike
            log.warning("SPIKE injected on %s: %.2f %s",
                        self.sensor_id, value, self.unit)

        # Clamp to physical bounds
        lo, hi = SENSOR_BOUNDS.get(self.sensor_type, (0, 9999))
        return round(max(lo, min(hi, value)), 3)

    def _build_payload(self, value: float) -> dict:
        return {
            "sensor_id":   self.sensor_id,
            "sensor_type": self.sensor_type,
            "location":    self.location,
            "value":       value,
            "unit":        self.unit,
            "quality":     quality_label(self.sensor_type, value),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

    def _send(self, payload: dict):
        """HTTP POST the reading to the fog node (retry once on failure)."""
        for attempt in range(2):
            try:
                resp = requests.post(
                    self.fog_node_url,
                    json=payload,
                    timeout=5,
                )
                if resp.status_code == 200:
                    log.info("%-18s → %6.3f %-7s [%s]",
                             self.sensor_id, payload["value"],
                             self.unit, payload["quality"])
                    return
                log.warning("Fog node returned %d for %s",
                            resp.status_code, self.sensor_id)
            except requests.exceptions.ConnectionError:
                if attempt == 0:
                    time.sleep(2)   # brief back-off before retry
                else:
                    log.error("Cannot reach fog node at %s — "
                              "is it running?", self.fog_node_url)
            except Exception as exc:
                log.error("Send error for %s: %s", self.sensor_id, exc)
                break

    def _run(self):
        """Main sensor loop: generate → send → sleep → repeat."""
        # Stagger startup so all sensors don't fire simultaneously
        time.sleep(random.uniform(0, self.dispatch_interval * 0.5))
        while not self._stop_event.is_set():
            value = self._generate_reading()
            payload = self._build_payload(value)
            self._send(payload)
            self._stop_event.wait(self.dispatch_interval)


# ── Entry point ────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    config_path = os.path.join(os.path.dirname(__file__), path)
    with open(config_path, "r") as fh:
        return yaml.safe_load(fh)


def main():
    cfg = load_config("sensor_config.yaml")

    # Allow overriding fog node URL via environment variable (for Docker)
    fog_url = os.getenv("FOG_NODE_URL", cfg["fog_node_url"])
    log.info("Sending sensor data to fog node at: %s", fog_url)

    sensors = [
        PollutionSensor(sensor_cfg, fog_url)
        for sensor_cfg in cfg["sensors"]
    ]

    for sensor in sensors:
        sensor.start()

    log.info("%d sensors running. Press Ctrl+C to stop.", len(sensors))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down sensors…")
        for sensor in sensors:
            sensor.stop()
        for sensor in sensors:
            sensor.join()
        log.info("All sensors stopped.")


if __name__ == "__main__":
    main()
