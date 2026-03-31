# Dockerfile for Naver Price Tracker
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY dashboard.html .
COPY targets.yaml .

# Create data directory
RUN mkdir -p data

# Environment variables
ENV PYTHONPATH="/app/src"
ENV PORT=8080

# Run the cloud entrypoint (UI server + Tracker)
CMD ["python", "-m", "tracker.cloud_app"]
