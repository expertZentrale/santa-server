ARG BASE_IMAGE=python:3.14-alpine3.23
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Santa Server" \
      org.opencontainers.image.description="Sync server for Santa, the binary authorization system for macOS" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/expertZentrale/santa-server"

WORKDIR /code

# mssql-python ships its own ODBC Driver 18, it only needs libltdl and kerberos at runtime.
# g++ / unixodbc-dev are only needed to build pyodbc (a dependency of mssql-django).
COPY requirements.txt .
RUN apk add --no-cache ca-certificates libssl3 libstdc++ libtool krb5-libs unixodbc; \
    apk add --no-cache --virtual .build-deps g++ unixodbc-dev krb5-dev; \
    pip install --no-cache-dir -r requirements.txt; \
    apk del .build-deps

COPY . .

# the static files are part of the image (served by WhiteNoise); the settings need some values to load
RUN SECRET_KEY=build DB_HOST=build DB_USER=build DB_PASSWORD=build REDIS_URL=redis://build \
    SANTA_PUBLIC_BASE_URL=http://build \
    python manage.py collectstatic --noinput \
    && chmod +x entrypoint.sh \
    && adduser -D -H -u 10001 santa

ENV PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=santa_server.settings
USER 10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

ENTRYPOINT ["/code/entrypoint.sh"]

# workers, threads and the rest: gunicorn.conf.py (environment variables GUNICORN_*)
CMD [ "gunicorn", "santa_server.wsgi" ]
