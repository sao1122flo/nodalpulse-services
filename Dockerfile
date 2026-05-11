FROM python:3.12-slim

WORKDIR /app

# Chromium runtime libraries (explicit list — more reproducible than --with-deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    fonts-liberation curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install uv --no-cache-dir

# Install Python dependencies (separate layer — only rebuilds when lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Pin the browser cache path so install and runtime resolve to the same location
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# Install Playwright Chromium binary (system libs already present above)
RUN uv run playwright install chromium

# Copy application source
COPY src/ ./src/
COPY Procfile ./

ENV PYTHONPATH=/app/src
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Default CMD — Railway overrides per service via Settings → Deploy → Start Command
CMD ["python", "-m", "nodalpulse.worker"]
