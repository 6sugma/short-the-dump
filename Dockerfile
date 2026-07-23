FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser
EXPOSE 8000
VOLUME ["/data"]

CMD ["short-the-dump", "--config", "config/default.toml", "--db", "/data/short_the_dump.db", "serve", "--host", "0.0.0.0", "--port", "8000"]
