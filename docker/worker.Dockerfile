FROM --platform=linux/arm64 python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ARG MISE_VERSION=v2026.10.19
ARG MISE_SHA256=4a4ca53b28e5433c527f4f73fac1f98f992a17cf32ca136a58e2e375f790f627

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates bash build-essential curl zstd \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/mise \
        "https://github.com/jdx/mise/releases/download/${MISE_VERSION}/mise-${MISE_VERSION}-linux-arm64" \
    && echo "${MISE_SHA256}  /usr/local/bin/mise" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/mise

RUN printf 'export PATH="$HOME/.local/share/mise/shims:$PATH"\n' > /etc/profile.d/mise.sh

COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md* LICENSE ./
RUN uv export --frozen --no-dev --extra aws --extra worker --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src ./src
RUN uv pip install --system --no-cache --no-deps . \
    && chown -R root:root /app && chmod -R a-w /app

RUN useradd --uid 1000 --create-home --home-dir /home/bed bed
USER 1000
ENV HOME=/home/bed \
    PYTHONUNBUFFERED=1 \
    MISE_DATA_DIR=/home/bed/.local/share/mise \
    MISE_CACHE_DIR=/home/bed/.cache/mise

EXPOSE 8080
CMD ["python", "-m", "bed.worker.app"]
