# Genie-V3 — FastAPI + the Playwright-driven discovery agent.
# A container, not a buildpack, because discovery drives a real Chromium.
FROM python:3.12-slim

WORKDIR /app

# Python deps first for better layer caching — this layer only rebuilds when
# requirements.txt changes, which the Chromium install below depends on.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + all its OS libraries (apt packages). This is the slow layer:
# expect ~10 min on a cold build. crawl4ai drives this same browser.
#
# Running as root is fine here: crawl4ai passes --no-sandbox and
# --disable-dev-shm-usage in its own default flags (browser_manager.py), which
# covers both the root-sandbox refusal and Docker's 64 MB /dev/shm, the two
# ways headless Chromium normally dies in a container.
RUN playwright install --with-deps chromium

# App code (see .dockerignore — .venv, node_modules, .git, runs/ etc. excluded).
COPY . .

ENV PYTHONUNBUFFERED=1

# The V3 service: CSV of organisations in, CSV of new portals out.
# Render/Railway inject $PORT; 8800 is the fallback for local `docker run`.
#
# Mutable state (runs/ and the three L5 memory JSONs) belongs on a mounted
# volume via AGENT_RUNS_DIR / GENIE_TNC_MEMORY / GENIE_DOMAIN_HISTORY /
# GENIE_BLOCK_FILE — anything written inside the image is lost on redeploy.
#
# The V2 genie app is still here and still runs; to serve it instead, override
# with: uvicorn genie.api.main:app --host 0.0.0.0 --port $PORT
CMD ["sh", "-c", "uvicorn service.api:app --host 0.0.0.0 --port ${PORT:-8800}"]
