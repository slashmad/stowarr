FROM python:3.14-slim
ARG STOWARR_COMMIT=unknown
LABEL org.opencontainers.image.revision="${STOWARR_COMMIT}"
ENV STOWARR_COMMIT="${STOWARR_COMMIT}"
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends 7zip gosu \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY docker/entrypoint.sh /usr/local/bin/stowarr-entrypoint
RUN chmod 0755 /usr/local/bin/stowarr-entrypoint
ENTRYPOINT ["stowarr-entrypoint"]
CMD ["stowarr", "serve"]
