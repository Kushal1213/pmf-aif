FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 \
    DATABASE_URL=sqlite:////srv/data/mf.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.1" \
    && useradd --system --uid 10001 app && mkdir -p /srv/data && chown app /srv/data
COPY app ./app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4).status == 200 else 1)"
# app.serve: restores the DB snapshot from $SNAPSHOT_REPO when SQLite is empty, then serves on $PORT.
# With DATABASE_URL pointing at PostgreSQL it simply serves.
CMD ["python", "-m", "app.serve"]
