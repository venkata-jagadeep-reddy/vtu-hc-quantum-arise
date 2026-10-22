"""Unit tests for circuit and optimizer modules."""

import sys
import math
import unittest
from pathlib import Path

# Add src to sys.path for testing
src_dir = str(Path(__file__).resolve().parent.parent / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

from circuit import create_sample_circuit, create_parameterized_circuit
from optimizer import run_optimization, cost_function


class TestQuantumArise(unittest.TestCase):

    def test_create_sample_circuit(self):
        circuit = create_sample_circuit(num_qubits=2)
        self.assertIsNotNone(circuit)

    def test_create_parameterized_circuit(self):
        circuit = create_parameterized_circuit(num_qubits=2, params=[0.5])
        self.assertIsNotNone(circuit)

    def test_cost_function(self):
        params = [0.5, 0.5]
        cost = cost_function(params)
        self.assertTrue(math.isclose(cost, 0.0, abs_tol=1e-5))

    def test_run_optimization(self):
        result = run_optimization(initial_params=[0.0, 0.0], method="COBYLA", max_iter=100)
        self.assertIn("success", result)
        self.assertIn("optimized_params", result)
        self.assertIn("final_cost", result)
        self.assertLessEqual(result["final_cost"], 0.2)


if __name__ == "__main__":
    unittest.main()
