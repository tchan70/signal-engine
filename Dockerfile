FROM python:3.12-slim

# Session 9: matches host logs; all trading logic uses America/New_York
# explicitly via utils/market_time.py, so TZ only affects log timestamps.
ENV TZ=Europe/London

WORKDIR /app

# Session 9: python:*-slim ships without pgrep, so the healthcheck below was
# permanently "unhealthy". procps provides it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Runtime directories
RUN mkdir -p logs

# Health check: verify the process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD pgrep -f "python main.py" || exit 1

CMD ["python", "main.py"]
