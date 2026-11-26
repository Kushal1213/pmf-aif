FROM python:3.12-slim
WORKDIR /srv
# HF_HOME on the writable data dir: the snapshot download (huggingface_hub + xet) needs a cache directory;
# the xet chunk cache is off so a restore doesn't keep a second copy of the snapshot on a small disk.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 \
    DATABASE_URL=sqlite:////srv/data/mf.db \
    HOME=/home/app HF_HOME=/srv/data/.hf HF_HUB_DISABLE_TELEMETRY=1 HF_XET_CHUNK_CACHE_SIZE_BYTES=0
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.1" \
    && useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /srv/data && chown app /srv/data
COPY app ./app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4).status == 200 else 1)"
# app.serve: restores the DB snapshot from $SNAPSHOT_REPO when SQLite is empty, then serves on $PORT.
# With DATABASE_URL pointing at PostgreSQL it simply serves.
CMD ["python", "-m", "app.serve"]
