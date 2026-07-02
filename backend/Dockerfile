# Wavr backend — lean base image (network + ruview + sim + fusion + rules/away + narration).
# The camera/GPU path (torch+cv2, [camera] extra) is a separate heavier variant — see docs.
FROM python:3.11-slim

# Non-root user (least privilege; the app needs no root).
RUN useradd --create-home --uid 10001 wavr
WORKDIR /app

# Install deps first (layer caching): copy only the package metadata + source.
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/wavr /app/backend/wavr
RUN pip install --no-cache-dir -e /app/backend

# Data dir for the SQLite DB (mounted as a volume at runtime).
RUN mkdir -p /data && chown -R wavr:wavr /data /app
USER wavr

ENV WAVR_DB=/data/wavr.db
EXPOSE 8000

# Bind loopback ONLY: with network_mode: host (Linux appliance) this keeps the in-app
# loopback-peer guard working. LAN access to the dashboard is via SSH tunnel.
CMD ["python", "-m", "uvicorn", "wavr.app:app", "--host", "127.0.0.1", "--port", "8000"]
