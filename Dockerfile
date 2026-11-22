FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --uid 1000 --create-home app && chown -R app /app
USER app
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 7000
CMD ["receptionist", "serve"]
