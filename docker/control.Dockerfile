FROM public.ecr.aws/lambda/python:3.12-arm64@sha256:5f3339c82f5fb9064e545acd23ea8b4fb080861643096dee3fb2e063d66a5734

COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md* LICENSE /build/
RUN cd /build && uv export --frozen --no-dev --extra aws --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
COPY src /build/src
RUN uv pip install --system --no-cache --no-deps /build && rm -rf /build
