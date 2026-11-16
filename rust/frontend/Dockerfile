# Build from the repository root. Runtime metadata is mounted by deploy-vps.py.
FROM rust:1.93.1-bookworm AS build
COPY deploy/install-trunk.sh /tmp/install-trunk.sh
RUN rustup target add wasm32-unknown-unknown \
    && sh /tmp/install-trunk.sh /usr/local/bin \
    && rm /tmp/install-trunk.sh
WORKDIR /app
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY rust ./rust
RUN trunk build --config rust/frontend/Trunk.toml --release --locked

FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY rust/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/rust/frontend/dist /usr/share/nginx/html
EXPOSE 8080
