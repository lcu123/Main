# ── QUO VOIP Connector — Remote MCP Server ────────────────────────────────────
# Deploys to Render (or any Docker host).
# Build:   docker build -t quo-voip-mcp .
# Run:     docker run -p 8000:8000 --env-file .env quo-voip-mcp

FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install Python dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install server-specific extras
RUN pip install --no-cache-dir \
    "fastapi>=0.111.0" \
    "uvicorn[standard]>=0.29.0" \
    "mcp>=1.0.0"

# Copy application code
COPY . .

# Install the package in editable mode so all sub-packages resolve correctly
RUN pip install --no-cache-dir -e ".[all]"

# Render injects PORT at runtime; default to 8000 for local dev
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Health check — Render uses this to verify the service is up
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

EXPOSE ${PORT}

# Start the remote MCP server
CMD ["sh", "-c", "uvicorn server.remote_mcp:app --host 0.0.0.0 --port ${PORT}"]
