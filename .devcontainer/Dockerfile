FROM python:3.14-alpine3.23

WORKDIR /code

COPY requirements.txt requirements-dev.txt ./
# git and gettext (makemessages / compilemessages) are development tools, the image only needs the committed .mo
RUN apk add --no-cache bash curl git gettext ca-certificates libssl3 libstdc++ libtool krb5-libs unixodbc; \
    apk add --no-cache --virtual .build-deps g++ unixodbc-dev krb5-dev; \
    pip install --no-cache-dir -r requirements-dev.txt; \
    apk del .build-deps


# an unprivileged user: files created in the workspace (git, collectstatic, migrations) belong to the host user,
# the devcontainer changes its UID/GID to the host's (updateRemoteUserUID)
RUN adduser -D -u 1000 -s /bin/bash dev
