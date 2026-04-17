"""
Smart City Pollution Monitoring System
Fog Node – Data Processor

Responsibilities:
  1. Buffer incoming sensor readings per sensor ID.
  2. Compute aggregates (mean, min, max, std) over the buffered window.
  3. Calculate the US-EPA Air Quality Index (AQI) for each reading.
  4. Detect anomalies using a configurable Z-score threshold.
"""

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── AQI breakpoint tables ──────────────────────────────────────────────────────
# Each entry: (C_low, C_high, AQI_low, AQI_high)
AQI_BREAKPOINTS: Dict[str, List[tuple]] = {
    "PM2.5": [
        (0.0,   12.0,   0,   50),
        (12.1,  35.4,  51,  100),
        (35.5,  55.4, 101,  150),
        (55.5, 150.4, 151,  200),
        (150.5, 250.4, 201, 300),
        (250.5, 350.4, 301, 400),
        (350.5, 500.4, 401, 500),
    ],
    "PM10": [
        (0,   54,   0,  50),
        (55,  154,  51, 100),
        (155, 254, 101, 150),
        (255, 354, 151, 200),
        (355, 424, 201, 300),
        (425, 504, 301, 400),
        (505, 604, 401, 500),
    ],
    "NO2": [
        (0,    53,   0,  50),
        (54,  100,  51, 100),
        (101, 360, 101, 150),
        (361, 649, 151, 200),
        (650, 1249, 201, 300),
        (1250, 1649, 301, 400),
        (1650, 2049, 401, 500),
    ],
    "CO": [
        (0.0,  4.4,   0,  50),
        (4.5,  9.4,  51, 100),
        (9.5,  12.4, 101, 150),
        (12.5, 15.4, 151, 200),
        (15.5, 30.4, 201, 300),
        (30.5, 40.4, 301, 400),
        (40.5, 50.4, 401, 500),
    ],
    "O3": [
        (0,   54,   0,  50),
        (55,  70,  51, 100),
        (71,  85, 101, 150),
        (86, 105, 151, 200),
        (106, 200, 201, 300),
    ],
}

AQI_CATEGORIES = [
    (50,  "Good",                    "#00e400"),
    (100, "Moderate",                "#ffff00"),
    (150, "Unhealthy for Sensitive", "#ff7e00"),
    (200, "Unhealthy",               "#ff0000"),
    (300, "Very Unhealthy",          "#8f3f97"),
    (500, "Hazardous",               "#7e0023"),
]


def calculate_aqi(sensor_type: str, concentration: float) -> Optional[int]:
    """
    Compute the US-EPA AQI for *concentration* of *sensor_type*.
    Returns None if the sensor type has no breakpoint table.

    Formula:
        AQI = ((AQI_hi - AQI_lo) / (C_hi - C_lo)) * (C - C_lo) + AQI_lo
    """
    breakpoints = AQI_BREAKPOINTS.get(sensor_type)
    if not breakpoints:
        return None

    for c_lo, c_hi, aqi_lo, aqi_hi in breakpoints:
        if c_lo <= concentration <= c_hi:
            aqi = ((aqi_hi - aqi_lo) / (c_hi - c_lo)) * (concentration - c_lo) + aqi_lo
            return round(aqi)

    # Beyond highest breakpoint → cap at 500
    return 500


def aqi_category(aqi: int) -> dict:
    """Return the category label and colour hex for a given AQI integer."""
    for threshold, label, colour in AQI_CATEGORIES:
        if aqi <= threshold:
            return {"label": label, "colour": colour}
    return {"label": "Hazardous", "colour": "#7e0023"}


# ── Sensor buffer ──────────────────────────────────────────────────────────────

@dataclass
class SensorBuffer:
    """
    Maintains a fixed-size rolling window of raw readings for one sensor.
    Provides aggregate statistics and anomaly detection.
    """
    sensor_id:    str
    sensor_type:  str
    location:     str
    unit:         str
    window_size:  int = 20
    z_threshold:  float = 2.5

    # Internal state
    _values:      deque = field(default_factory=deque, init=False, repr=False)
    _last_raw:    Optional[float] = field(default=None, init=False, repr=False)
    _last_ts:     Optional[str]   = field(default=None, init=False, repr=False)
    _last_quality: Optional[str]  = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._values = deque(maxlen=self.window_size)

    def add(self, value: float, timestamp: str, quality: str):
        """Push a new reading into the rolling window."""
        self._values.append(value)
        self._last_raw    = value
        self._last_ts     = timestamp
        self._last_quality = quality

    def aggregates(self) -> dict:
        """Return statistical aggregates over the current window."""
        vals = list(self._values)
        if not vals:
            return {}
        mean = statistics.mean(vals)
        std  = statistics.stdev(vals) if len(vals) > 1 else 0.0
        return {
            "mean":  round(mean, 3),
            "min":   round(min(vals), 3),
            "max":   round(max(vals), 3),
            "std":   round(std, 3),
            "count": len(vals),
        }

    def is_anomaly(self, value: float) -> bool:
        """
        Flag a reading as an anomaly if it deviates more than
        z_threshold standard deviations from the rolling mean.
        Requires at least 5 readings to make a reliable judgement.
        """
        vals = list(self._values)
        if len(vals) < 5:
            return False
        mean = statistics.mean(vals)
        std  = statistics.stdev(vals)
        if std == 0:
            return False
        z_score = abs(value - mean) / std
        return z_score > self.z_threshold

    def flush(self) -> dict:
        """
        Build the processed payload for this sensor and clear the buffer.
        Returns an empty dict if no readings are available.
        """
        if self._last_raw is None:
            return {}

        agg      = self.aggregates()
        anomaly  = self.is_anomaly(self._last_raw)
        aqi_val  = calculate_aqi(self.sensor_type, agg.get("mean", self._last_raw))

        result = {
            "sensor_id":   self.sensor_id,
            "sensor_type": self.sensor_type,
            "location":    self.location,
            "unit":        self.unit,
            "raw_value":   self._last_raw,
            "quality":     self._last_quality,
            "aggregates":  agg,
            "aqi":         aqi_val,
            "aqi_category": aqi_category(aqi_val) if aqi_val is not None else None,
            "anomaly":     anomaly,
            "timestamp":   self._last_ts,
        }

        # Clear the buffer after flush
        self._values.clear()
        self._last_raw    = None
        self._last_ts     = None
        self._last_quality = None

        return result


# ── Buffer registry ────────────────────────────────────────────────────────────

class BufferRegistry:
    """
    Thread-safe registry of SensorBuffers keyed by sensor_id.
    Auto-creates a buffer on first encounter of a new sensor ID.
    """

    def __init__(self, window_size: int = 20, z_threshold: float = 2.5):
        self._buffers: Dict[str, SensorBuffer] = {}
        self._window_size = window_size
        self._z_threshold = z_threshold

    def record(self, reading: dict):
        """Add a sensor reading to its corresponding buffer."""
        sid = reading["sensor_id"]
        if sid not in self._buffers:
            self._buffers[sid] = SensorBuffer(
                sensor_id   = sid,
                sensor_type = reading["sensor_type"],
                location    = reading.get("location", "unknown"),
                unit        = reading.get("unit", ""),
                window_size = self._window_size,
                z_threshold = self._z_threshold,
            )
        self._buffers[sid].add(
            reading["value"],
            reading["timestamp"],
            reading.get("quality", "unknown"),
        )

    def flush_all(self) -> List[dict]:
        """Flush all non-empty buffers and return the processed readings."""
        results = []
        for buf in self._buffers.values():
            payload = buf.flush()
            if payload:
                results.append(payload)
        return results

    @property
    def sensor_count(self) -> int:
        return len(self._buffers)
