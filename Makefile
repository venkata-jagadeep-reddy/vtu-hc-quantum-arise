# Convenience targets. Everything here is a thin wrapper over the CLI so that
# the commands in the README stay the single source of truth.
#
#   make install     create .venv and install dependencies
#   make test        run the test suite
#   make calibrate   build data/baseline.json
#   make check       one heartbeat verification of the healthy circuit
#   make demo        inject g00:replace:x, detect and localise it
#   make evaluate    full evaluation matrix (slow: ~30-45 min)
#   make summarize   render the saved evaluation as markdown tables
#   make dashboard   launch the Streamlit UI
#   make clean       remove caches and generated data

PY      := PYTHONPATH=src .venv/bin/python
PIP     := .venv/bin/pip
COLOUR  := \033[1;36m
RESET   := \033[0m

.PHONY: help install test calibrate check demo evaluate summarize dashboard report reference clean

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

install:
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

test:
	$(PY) -m pytest -q

reference:
	$(PY) -m quantumpulse.cli reference

calibrate:
	$(PY) -m quantumpulse.cli calibrate --runs 200 --shots 4096 --output data/baseline.json

check:
	$(PY) -m quantumpulse.cli check

demo:
	$(PY) -m quantumpulse.cli localize --tamper g00:replace:x --exact-sweep

evaluate:
	$(PY) -m quantumpulse.cli evaluate \
		--profiles ideal depolarizing_mild depolarizing_strong thermal \
		--runs 200 --trials 3 --nominal-trials 100 --localization-trials 2 \
		--shots 4096 --exact-sweep --output data/evaluation

summarize:
	$(PY) -m quantumpulse.cli summarize --input data/evaluation/evaluation.json

dashboard:
	PYTHONPATH=src .venv/bin/streamlit run src/quantumpulse/dashboard.py

report:
	$(PY) -m quantumpulse.cli report

clean:
	rm -rf .pytest_cache src/**/__pycache__ tests/__pycache__
	rm -f data/quantumpulse.db
