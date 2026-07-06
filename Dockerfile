# Genie backend — FastAPI + the Playwright-driven discovery agent.
# Built as a container because live discovery drives a real Chromium browser.
FROM python:3.12-slim

WORKDIR /app

# Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + all its OS libraries (apt packages). Required for /discover.
RUN playwright install --with-deps chromium

# App code (see .dockerignore — .venv, node_modules, .git etc. are excluded).
COPY . .

ENV PYTHONUNBUFFERED=1

# Render/Railway inject $PORT; fall back to 8799 for local `docker run`.
CMD ["sh", "-c", "uvicorn genie.api.main:app --host 0.0.0.0 --port ${PORT:-8799}"]
