FROM python:3.12-slim

# System deps (lxml needs libxml2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev libxslt1-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source (config/data are volume-mounted at runtime — see docker-compose.yml)
# Ensure config dir exists as a directory before COPY so it can never become a file
RUN mkdir -p /app/config
COPY . .

# Exclude local dev artifacts
RUN rm -rf .venv __pycache__ data node_modules

# Create data dir
RUN mkdir -p data

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]
