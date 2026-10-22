"""QuantumPulse: heartbeat-based quantum circuit integrity monitoring.

The package is organised as a pipeline that mirrors the architecture flow
described in the project master context:

    program -> execution -> heartbeat verification -> anomaly detection
            -> (anomaly) gate-tampering localization -> integrity report

Every module is usable on its own so that each stage can be unit tested and
evaluated independently.
"""

from .config import MonitorConfig, NoiseConfig
from .program import Checkpoint, GateSpec, QuantumPulseProgram

__all__ = [
    "MonitorConfig",
    "NoiseConfig",
    "Checkpoint",
    "GateSpec",
    "QuantumPulseProgram",
]

__version__ = "0.1.0"
