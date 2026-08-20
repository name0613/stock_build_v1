FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
ARG SOURCE_REVISION=unknown
ARG BACKEND_LOCK_SHA256=unknown
ARG SCORE_SPEC_HASH=unknown
ARG CALENDAR_HASH=unknown
ARG BUILD_TIMESTAMP=unknown
LABEL org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.source-lock-sha256="$BACKEND_LOCK_SHA256" \
      org.openai.score-spec-sha256="$SCORE_SPEC_HASH" \
      org.openai.calendar-sha256="$CALENDAR_HASH" \
      org.opencontainers.image.created="$BUILD_TIMESTAMP"
COPY backend/requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && printf '{"source_revision":"%s","backend_lock_sha256":"%s","score_spec_hash":"%s","calendar_hash":"%s","build_timestamp":"%s"}\n' "$SOURCE_REVISION" "$BACKEND_LOCK_SHA256" "$SCORE_SPEC_HASH" "$CALENDAR_HASH" "$BUILD_TIMESTAMP" > /app/build-metadata.json
COPY backend/app ./app
COPY backend/tests ./tests
COPY migrations ./migrations
RUN addgroup --gid 1000 app && adduser --uid 1000 --gid 1000 --disabled-password --gecos "" app \
    && mkdir -p /data/raw && chown -R app:app /app /data/raw
USER app
EXPOSE 8000 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
