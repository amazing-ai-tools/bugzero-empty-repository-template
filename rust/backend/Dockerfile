# Build from the repository root: docker build -f rust/backend/Dockerfile .
FROM rust:1.93.1-bookworm AS build
WORKDIR /app
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY rust ./rust
RUN cargo build --locked --release --package app-api

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home app
COPY --from=build /app/target/release/app-api /usr/local/bin/app-api
USER app
ENV BIND_ADDR=0.0.0.0:8000
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/app-api"]
