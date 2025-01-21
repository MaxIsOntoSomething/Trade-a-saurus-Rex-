FROM python:3.9-slim

# Set noninteractive installation
ENV DEBIAN_FRONTEND=noninteractive

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    tzdata \
    python3-tk \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set timezone
ENV TZ=UTC
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Install Python dependencies first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set correct permissions for mounted volumes
RUN mkdir -p /app/logs /app/data /app/config && \
    chown -R 1000:1000 /app/logs /app/data /app/config && \
    chmod 755 /app/logs /app/data /app/config

# Add non-root user
RUN useradd -u 1000 -ms /bin/bash botuser && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Copy configuration files first
COPY config/* /app/config/

# Copy the rest of the application
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DOCKER=true \
    WEBSOCKET_TIMEOUT=60 \
    WEBSOCKET_RETRY_DELAY=5 \
    MAX_RECONNECT_ATTEMPTS=10

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/health')"

# Run the bot
CMD ["python", "main.py"]
