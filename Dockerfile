# syntax=docker/dockerfile:1
#
# Stages:
#   dev         uv and every dependency group. docker-compose.yaml and
#               docker-compose.test.yaml mount the working tree over /app.
#   production  the app and its runtime dependencies only, as a non-root
#               user. Released to ghcr.io by .github/workflows/release.yaml.
#
# The python tag matches .python-version and requires-python in
# pyproject.toml; change all three together.

FROM python:3.15-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/venv \
    PATH=/venv/bin:$PATH \
    HOST=0.0.0.0
WORKDIR /app

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
# Dependencies first, so a source change doesn't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM build AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
CMD ["python", "-m", "zigbee2mqtt_secure_proxy"]

FROM base AS production
# Numeric, so orchestrators can verify it isn't root.
RUN groupadd --system --gid 10001 app \
 && useradd --system --gid 10001 --uid 10001 --no-log-init --no-create-home app
COPY --from=build /venv /venv
USER 10001:10001
EXPOSE 8080
CMD ["zigbee2mqtt-secure-proxy"]
