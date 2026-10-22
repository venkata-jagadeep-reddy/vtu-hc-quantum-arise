"""Integrity report generation.

The report is the artefact a human actually reads after an alert, so it is
built to be honest about three separate questions:

1. *Was a deviation detected?*  (heartbeat verification)
2. *How confident are we, and what is the statistical evidence?*  (anomaly
   score, p-values, thresholds, noise floor)
3. *Where is the most likely modification, and what supports that claim?*
   (ranked candidates, the winning hypothesis, the margin to the runner-up)

The report never labels a deviation as "proof of tampering"; it states that a
deviation was detected and lists the evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .execution import HeartbeatResult
from .localization import LocalizationReport
from .verification import IntegrityVerdict

STATUS_OK = "INTEGRITY OK"
STATUS_ALERT = "DEVIATION DETECTED"


@dataclass
class IntegrityReport:
    """Human- and machine-readable summary of one monitoring round."""

    timestamp: str
    program_name: str
    program_fingerprint: str
    backend: Dict[str, Any]
    shots: int
    status: str
    verdict: IntegrityVerdict
    localization: Optional[LocalizationReport] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        return self.verdict.flagged

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "program_name": self.program_name,
            "program_fingerprint": self.program_fingerprint,
            "backend": self.backend,
            "shots": self.shots,
            "status": self.status,
            "flagged": self.flagged,
            "verdict": self.verdict.to_dict(),
            "localization": self.localization.to_dict() if self.localization else None,
            "metadata": self.metadata,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_text(self) -> str:
        lines: List[str] = []
        rule = "=" * 78
        lines.append(rule)
        lines.append(f"QuantumPulse Integrity Report - {self.timestamp}")
        lines.append(rule)
        lines.append(f"Program        : {self.program_name}  (fp={self.program_fingerprint})")
        lines.append(f"Backend        : {self.backend.get('backend')} / noise={self.backend.get('noise', {}).get('name')}")
        lines.append(f"Shots          : {self.shots}")
        lines.append(f"Status         : {self.status}")
        lines.append("")
        lines.append("-- Heartbeat verification " + "-" * 49)
        header = f"  {'checkpoint':<10} {'D_TV':>9} {'threshold':>10} {'p(null)':>9} {'score':>7}  verdict"
        lines.append(header)
        for v in self.verdict.verdicts:
            flag = "ALERT" if v.flagged else "ok"
            lines.append(
                f"  {v.cp_id:<10} {v.observed_tvd:>9.5f} {v.threshold:>10.5f} "
                f"{v.null_p_value:>9.4f} {v.anomaly_score:>7.3f}  {flag}"
            )
        lines.append("")
        for note in self.verdict.notes:
            lines.append(f"  * {note}")

        if self.localization and self.localization.candidates:
            lines.append("")
            lines.append("-- Suspected locations " + "-" * 50)
            bound = self.localization.search_bound_checkpoint or self.localization.target_checkpoint
            lines.append(
                f"  Search space : {len(self.localization.suspect_segment)} gate(s) preceding "
                f"{bound}"
            )
            lines.append(
                f"  Evidence     : {len(self.localization.checkpoints_used)} deviating "
                f"checkpoint(s) fused ({', '.join(self.localization.checkpoints_used)})"
            )
            if self.localization.ambiguous:
                lines.append(
                    "  Ambiguity    : several modifications are statistically "
                    "indistinguishable (" + ", ".join(self.localization.tied_gids) + ")"
                )
            lines.append(
                f"  Top-1        : {self.localization.top1_gid} "
                f"(confidence {self.localization.confidence:.3f}, margin {self.localization.margin:.2f})"
            )
            lines.append("")
            for c in self.localization.candidates[:5]:
                mark = " <== true location" if c.is_true_location else ""
                lines.append(
                    f"  #{c.rank} {c.gid} ({c.gate_description}) stage={c.stage} "
                    f"D_TV={c.tvd_to_observed:.5f} score={c.score:.3f}{mark}"
                )
                lines.append(f"      hypothesis: {c.hypothesis_description}")
        elif self.verdict.flagged:
            lines.append("")
            lines.append("-- Suspected locations " + "-" * 50)
            lines.append("  Localisation was not run for this round.")

        lines.append("")
        lines.append(rule)
        return "\n".join(lines)


def build_report(
    program,
    verdict: IntegrityVerdict,
    heartbeat: Optional[HeartbeatResult] = None,
    localization: Optional[LocalizationReport] = None,
    backend_description: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> IntegrityReport:
    """Assemble an :class:`IntegrityReport` from the pipeline outputs."""

    status = STATUS_ALERT if verdict.flagged else STATUS_OK
    meta = dict(metadata or {})
    if heartbeat is not None:
        meta.setdefault("execution_seconds", heartbeat.total_seconds)
        meta.setdefault("seed", heartbeat.seed)

    return IntegrityReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        program_name=program.name,
        program_fingerprint=program.fingerprint(),
        backend=backend_description or {},
        shots=heartbeat.shots if heartbeat is not None else 0,
        status=status,
        verdict=verdict,
        localization=localization,
        metadata=meta,
    )
