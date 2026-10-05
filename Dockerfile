# JARVIS backend + built frontend, one image.
#
# Voice models (models/) and the data snapshot (data/snapshot.db) are intentionally NOT baked
# in here -- they're large (~400MB / several MB respectively), rebuild-independent, and belong
# on a named volume so a code change doesn't force re-downloading them. Run the one-time init
# steps from DEPLOY.md after the stack is up.

# ---- stage 1: build the frontend ------------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- stage 2: the Python runtime ------------------------------------------------------------
FROM python:3.11-slim AS runtime

# faster-whisper/av need ffmpeg's shared libs; phonemizer/piper need espeak-ng's data files.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libsndfile1 \
      espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /src/frontend/dist ./frontend/dist

# Populated by the init steps in DEPLOY.md, kept on named volumes in docker-compose.yml.
VOLUME ["/app/data", "/app/models"]

EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
