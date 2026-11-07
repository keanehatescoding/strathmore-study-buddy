# Pinned to a patch release; bump deliberately (and rebuild for OS fixes).
ARG PYTHON_IMAGE=docker.io/library/python:3.12.14-slim-trixie

FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

# Dependencies only, from uv.lock: this layer is rebuilt when pyproject.toml
# or uv.lock change, not on every code change.
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-cache

FROM ${PYTHON_IMAGE}
RUN useradd --system --uid 10001 --no-create-home --home-dir /srv app
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /srv
# The app runs from the source tree (uvicorn/alembic/python -m import from
# the working directory), so the project itself isn't installed.
COPY alembic.ini ./
COPY alembic/ alembic/
COPY templates/ templates/
COPY static/ static/
COPY app/ app/
USER app
# The commit this image was built from, for /version and the startup logs.
# cloudbuild.yaml passes --build-arg GIT_COMMIT_SHA=$COMMIT_SHA; a plain
# `docker build` leaves it empty and the app reports "unknown" (buildah may
# reuse a cached layer from an earlier build that had the arg; --no-cache
# avoids that). Last, so a new commit doesn't invalidate the layers above.
ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

# Platforms with their own checks (Cloud Run) ignore this; plain docker/podman
# use it. Non-web services started from this image (worker, crons) should
# override it with --no-healthcheck / healthcheck.disable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)"

CMD alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
