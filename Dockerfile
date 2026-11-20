FROM python:3.15.2-slim-trixie
ARG STOWARR_COMMIT=unknown
LABEL org.opencontainers.image.revision="${STOWARR_COMMIT}"
ENV STOWARR_COMMIT="${STOWARR_COMMIT}"
ENV STOWARR_PORT=8788 STOWARR_WEB_PORT=8080
EXPOSE 8080 8788
WORKDIR /app
# Debian packages the RAR/RAR5 decoders separately from 7zip.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends 7zip 7zip-rar gosu \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY docker/entrypoint.sh /usr/local/bin/stowarr-entrypoint
RUN chmod 0755 /usr/local/bin/stowarr-entrypoint
ENTRYPOINT ["stowarr-entrypoint"]
CMD ["stowarr", "serve"]
