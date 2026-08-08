# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 AS uv

FROM python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 documind \
    && useradd --create-home --home-dir /app --uid 10001 --gid documind --shell /usr/sbin/nologin documind

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY --chown=documind:documind src ./src
RUN uv sync --frozen --no-dev --no-cache \
    && chown -R documind:documind /app

USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "documind.main:app", "--host", "0.0.0.0", "--port", "8000"]
