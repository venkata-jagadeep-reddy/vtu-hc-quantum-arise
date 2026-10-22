"""Periodic heartbeat monitoring loop.

The monitoring session repeatedly executes the circuit, verifies every
heartbeat against the calibrated baseline and stores the outcome.  Anomaly
reports are raised the moment a checkpoint deviates beyond the calibrated
threshold.

The loop is intentionally simple and synchronous: it is a prototype, and the
value lies in the verifiable statistics rather than in concurrency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .backends import AerBackend
from .calibration import BaselineProfile
from .config import MonitorConfig
from .evidence import IntegrityReport, build_report
from .execution import run_heartbeats
from .localization import localize
from .program import QuantumPulseProgram
from .storage import MonitoringStore
from .verification import VerificationConfig, verify_heartbeats


@dataclass
class MonitorSession:
    """A running heartbeat monitor."""

    program: QuantumPulseProgram
    """The circuit under surveillance."""

    backend: AerBackend
    baseline: BaselineProfile
    config: MonitorConfig = field(default_factory=MonitorConfig)
    verification_config: VerificationConfig = field(default_factory=VerificationConfig)
    reference: Optional[QuantumPulseProgram] = None
    """The expected circuit used as the base for localisation hypotheses.

    Defaults to the circuit under surveillance, which is correct when the
    deployed circuit is expected to match the baseline exactly.  Set it
    explicitly when the monitored circuit may legitimately differ from the
    reference.
    """

    store: Optional[MonitoringStore] = None
    on_report: Optional[Callable[[IntegrityReport], None]] = None
    iteration: int = 0

    def check_once(
        self,
        label: str = "nominal",
        tamper_id: Optional[str] = None,
        tamper_target: Optional[str] = None,
        with_localization: bool = True,
    ) -> IntegrityReport:
        """Execute one heartbeat round and produce an integrity report."""

        seed = None if self.config.seed is None else self.config.seed + self.iteration
        heartbeat = run_heartbeats(
            self.program, self.backend, shots=self.config.shots, seed=seed
        )
        verdict = verify_heartbeats(
            heartbeat.observations,
            self.baseline,
            self.verification_config,
            program=self.program,
        )

        localization = None
        if verdict.flagged and with_localization:
            flagged_observations = [
                heartbeat.by_id(v.cp_id) for v in verdict.verdicts if v.flagged
            ]
            if flagged_observations:
                localization = localize(
                    self.reference or self.program,
                    flagged_observations,
                    self.baseline,
                    self.backend,
                    shots=self.config.shots,
                    seed=seed,
                )

        report = build_report(
            self.program,
            verdict,
            heartbeat=heartbeat,
            localization=localization,
            backend_description=self.backend.describe(),
            metadata={"iteration": self.iteration, "seed": seed},
        )

        if self.store is not None:
            self.store.record_run(
                report.to_dict(),
                label=label,
                tamper_id=tamper_id,
                tamper_target=tamper_target,
            )

        if self.on_report is not None:
            self.on_report(report)

        self.iteration += 1
        return report

    def run(
        self,
        max_iterations: Optional[int] = None,
        sleep: bool = True,
    ) -> List[IntegrityReport]:
        """Run the monitoring loop until the iteration limit is reached."""

        limit = self.config.max_iterations if max_iterations is None else max_iterations
        reports: List[IntegrityReport] = []
        while limit <= 0 or self.iteration < limit:
            reports.append(self.check_once())
            if sleep and (limit <= 0 or self.iteration < limit):
                time.sleep(self.config.interval_seconds)
        return reports
