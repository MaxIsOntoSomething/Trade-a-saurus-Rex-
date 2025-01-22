FROM python:3.11-slim as builder

# Build arguments
ARG APP_USER=botuser
ARG UID=1000
ARG GID=1000

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create application directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Final stage
FROM python:3.11-slim

# Import build arguments
ARG APP_USER
ARG UID
ARG GID

# Set working directory
WORKDIR /app

# Create non-root user and required directories
RUN groupadd -g $GID $APP_USER && \
    useradd -m -u $UID -g $GID -s /bin/bash $APP_USER && \
    mkdir -p data/backups logs config && \
    chown -R $APP_USER:$APP_USER /app && \
    chmod -R u+rwx /app/logs && \
    chmod -R u+rwx /app/data

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy application files with correct ownership
COPY --chown=$APP_USER:$APP_USER . .

# Set proper permissions
RUN chmod -R u=rwX,g=rX,o= /app && \
    chmod -R u=rwX,g=rwX,o= /app/data /app/logs && \
    chown -R $APP_USER:$APP_USER /app/logs /app/data

# Switch to non-root user
USER $APP_USER

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys, os; sys.exit(0 if os.path.exists('/app/data/trades.json') else 1)"

# Start bot
CMD ["python", "main.py"]
