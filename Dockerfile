FROM python:3.12-slim

WORKDIR /app

# Install dependencies as root (needed for system-level pip installs), then
# drop to an unprivileged user before running the application. Running the
# API process as root inside the container is unnecessary privilege for a
# process that never needs it, and is flagged by most container scanners.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd --system escrow && useradd --system --gid escrow --no-create-home escrow \
    && chown -R escrow:escrow /app
USER escrow

EXPOSE 8000

# Uses the /health endpoint added alongside this change. Failing health
# checks let an orchestrator (Docker, Compose, Kubernetes) restart or stop
# routing traffic to a stuck instance instead of finding out from alerts.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else sys.exit(1)"

# WEB_CONCURRENCY controls the number of uvicorn worker processes. Note this
# app relies on in-process state for RateLimiter/VelocityGuard/USSD-session
# TTL bookkeeping is DB-backed (fine across workers/instances), but the
# in-memory limiter and velocity guard are NOT shared across workers or
# instances — see CORRECTIONS.md's "Redis-backed distributed rate limiting"
# item. Running >1 worker/instance today makes those checks per-worker
# rather than global, which is a real gap to close before scaling out, not
# something this change fixes.
ENV WEB_CONCURRENCY=1
CMD ["sh", "-c", "uvicorn escrow_main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY}"]
