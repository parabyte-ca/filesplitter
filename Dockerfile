FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ARG VERSION=unknown
LABEL org.opencontainers.image.title="FileSplitter" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Video encoder and scene splitter for media libraries"

EXPOSE 4250

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4250/api/version')" || exit 1

CMD ["python", "app.py"]
