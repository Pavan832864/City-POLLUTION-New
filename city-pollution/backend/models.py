"""
Smart City Pollution Monitoring System
Backend – SQLAlchemy ORM Models
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String

from database import Base


class SensorReading(Base):
    """
    Stores one processed reading per sensor per fog-dispatch cycle.
    Each row contains the raw value, rolling aggregates, AQI, and
    an anomaly flag computed by the fog node.
    """
    __tablename__ = "sensor_readings"

    id           = Column(Integer, primary_key=True, autoincrement=True)

    # Identity
    sensor_id    = Column(String(64),  nullable=False, index=True)
    sensor_type  = Column(String(32),  nullable=False, index=True)
    location     = Column(String(128), nullable=False)
    unit         = Column(String(16),  nullable=False)
    fog_node_id  = Column(String(64),  nullable=False)

    # Values
    raw_value    = Column(Float, nullable=False)
    quality      = Column(String(32))

    # Aggregates computed by fog node
    agg_mean     = Column(Float)
    agg_min      = Column(Float)
    agg_max      = Column(Float)
    agg_std      = Column(Float)
    agg_count    = Column(Integer)

    # AQI
    aqi          = Column(Integer)
    aqi_label    = Column(String(64))
    aqi_colour   = Column(String(16))

    # Anomaly flag
    anomaly      = Column(Boolean, default=False)

    # Timestamps
    sensor_ts    = Column(DateTime, nullable=False)   # when sensor measured it
    ingested_at  = Column(DateTime, default=datetime.utcnow)  # when backend stored it

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "sensor_id":   self.sensor_id,
            "sensor_type": self.sensor_type,
            "location":    self.location,
            "unit":        self.unit,
            "fog_node_id": self.fog_node_id,
            "raw_value":   self.raw_value,
            "quality":     self.quality,
            "aggregates": {
                "mean":  self.agg_mean,
                "min":   self.agg_min,
                "max":   self.agg_max,
                "std":   self.agg_std,
                "count": self.agg_count,
            },
            "aqi": {
                "value":  self.aqi,
                "label":  self.aqi_label,
                "colour": self.aqi_colour,
            },
            "anomaly":     self.anomaly,
            "sensor_ts":   self.sensor_ts.isoformat() if self.sensor_ts else None,
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
        }
