FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

WORKDIR /app

RUN pip install uv --no-cache-dir

# Install Python dependencies (separate layer — only rebuilds when lockfile changes)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --python 3.12

# Copy application source
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY Procfile ./

ENV PYTHONPATH=/app/src
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Default CMD — Railway overrides per service via Settings → Deploy → Start Command
CMD ["python", "-m", "nodalpulse.worker"]
