# QuantumPulse prototype image.
#
#   docker build -t quantumpulse .
#   docker run --rm quantumpulse                       # prints the reference circuit
#   docker run --rm quantumpulse calibrate --runs 200  # build a baseline (writes inside the container)
#   docker run --rm -p 8501:8501 -v "$PWD/data:/app/data" quantumpulse \
#       streamlit run src/quantumpulse/dashboard.py --server.address 0.0.0.0
#
# Mount ./data to persist the baseline, the SQLite history and evaluation output.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first so that source edits do not invalidate this layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install -e . --no-deps

# The reference program is code; the baseline/history are data and are created
# at runtime (or mounted from the host).
RUN mkdir -p /app/data

# Non-root runtime user.
RUN useradd --create-home --uid 10001 monitor && chown -R monitor:monitor /app
USER monitor

CMD ["quantumpulse", "reference"]
