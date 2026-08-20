FROM node:20-alpine AS build
ARG SOURCE_REVISION=unknown
ARG FRONTEND_LOCK_SHA256=unknown
ARG BUILD_TIMESTAMP=unknown
WORKDIR /app
COPY package.json package-lock.json tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm ci --no-audit --no-fund && npm run build

FROM nginx:1.27-alpine
ARG SOURCE_REVISION=unknown
ARG FRONTEND_LOCK_SHA256=unknown
ARG BUILD_TIMESTAMP=unknown
LABEL org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.source-lock-sha256="$FRONTEND_LOCK_SHA256" \
      org.opencontainers.image.created="$BUILD_TIMESTAMP"
COPY --from=build /app/dist /usr/share/nginx/html
RUN printf '{"source_revision":"%s","frontend_lock_sha256":"%s","build_timestamp":"%s"}\n' "$SOURCE_REVISION" "$FRONTEND_LOCK_SHA256" "$BUILD_TIMESTAMP" > /usr/share/nginx/html/build-metadata.json
EXPOSE 80
