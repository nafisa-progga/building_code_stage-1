# ─────────────────────────────────────────────────────────────
#  Building Code Web Project — Dockerfile
#  Runs both FastAPI (port 8000) and Streamlit (port 8501)
#  inside a single container using supervisord.
# ─────────────────────────────────────────────────────────────

FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────
RUN apt-get update && apt-get install -y \
    supervisor \
    curl \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────
WORKDIR /app

# ── Install Python dependencies ───────────────────────────────
# Copy requirements first so Docker can cache this layer.
# If requirements.txt doesn't change, pip install is skipped
# on the next build — much faster rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy project source code ──────────────────────────────────
COPY . .

# ── Create required directories ───────────────────────────────
RUN mkdir -p storage/output storage/figures

# ── Copy supervisord config ───────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# ── Streamlit config ──────────────────────────────────────────
RUN mkdir -p /root/.streamlit
COPY .streamlit/config.toml /root/.streamlit/config.toml

# ── Expose ports ──────────────────────────────────────────────
# 8501 → Streamlit viewer (main app for the client)
# 8000 → FastAPI REST API
EXPOSE 8501 8000

# ── Health check — confirms Streamlit is up ───────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# ── Start both services via supervisord ───────────────────────
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]