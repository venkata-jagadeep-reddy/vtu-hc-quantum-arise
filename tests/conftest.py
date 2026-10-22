"""Shared pytest fixtures for the QuantumPulse test suite."""

from __future__ import annotations

import pytest

from quantumpulse.backends import AerBackend
from quantumpulse.calibration import BaselineProfile, calibrate
from quantumpulse.circuits import reference_program
from quantumpulse.config import NoiseConfig


@pytest.fixture(scope="session")
def program():
    return reference_program()


@pytest.fixture(scope="session")
def ideal_backend():
    return AerBackend(noise_config=NoiseConfig(name="ideal"))


@pytest.fixture(scope="session")
def baseline(program, ideal_backend) -> BaselineProfile:
    """A deliberately small calibration so the suite stays fast."""

    return calibrate(
        program,
        ideal_backend,
        shots=2048,
        n_runs=24,
        alpha=0.02,
        seed=1234,
    )
