FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
# Install required Linux system libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium and its dependencies
RUN python -m playwright install --with-deps chromium

# Clean up
RUN rm -rf /root/.cache/pip /var/lib/apt/lists/*

COPY main.py ./
COPY routes.json ./

EXPOSE 8000

CMD ["python", "main.py"]
