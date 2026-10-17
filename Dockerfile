# SehatDesk server image: staging, demos and future hosted/multi-branch servers.
# Hospitals still get the Windows/macOS installers (docs/PROJECT_NOTES.md §3); this image is
# the same app on Linux with PostgreSQL or SQLite.
#   docker build -t sehatdesk .
#   docker compose up            (PostgreSQL + app, see docker-compose.yml)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SEHATDESK_DATA_DIR=/data \
    SEHATDESK_PORT=8000

# Pango/Cairo for WeasyPrint PDFs (lab reports, report exports); pg_dump for backups.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 \
       libharfbuzz0b libfontconfig1 fonts-dejavu-core curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-postgres.txt ./
RUN pip install -r requirements-postgres.txt

COPY . .
# Static files are collected at build time; WhiteNoise serves them.
RUN DJANGO_SECRET_KEY=build-only-not-secret SEHATDESK_DATA_DIR=/tmp/build-data \
    python manage.py collectstatic --noinput -v0

RUN useradd --system --uid 1000 --home /app sehatdesk \
    && mkdir -p /data && chown sehatdesk /data
USER sehatdesk
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/health/ready/ || exit 1

# Migrations run on start, as the desktop launcher does.
CMD ["sh", "-c", "python manage.py migrate --noinput && python serve.py --host 0.0.0.0 --port ${SEHATDESK_PORT}"]
