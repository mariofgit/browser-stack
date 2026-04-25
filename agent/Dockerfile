FROM python:3.12-slim

# Chromium dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Install Chromium for Playwright
RUN playwright install chromium

COPY runtime/ ./runtime/

EXPOSE 8012
CMD ["uvicorn", "runtime.app:app", "--host", "0.0.0.0", "--port", "8012"]
