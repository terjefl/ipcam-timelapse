FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY timelapse.py .

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request, sys; r=urllib.request.urlopen('http://localhost:8080/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"

CMD ["python", "-u", "timelapse.py"]
