#!/bin/sh
# Pinned official Linux x86_64 Trunk release used by CI and the frontend image.
set -eu
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
  echo 'The Rust VPS template requires a Linux x86_64 build runner.' >&2
  exit 1
fi
trunk_destination="${1:-${HOME:?}/.cargo/bin}"
trunk_download_dir="$(mktemp -d)"
trap 'rm -rf "$trunk_download_dir"' EXIT HUP INT TERM
curl --fail --location --retry 3 \
  https://github.com/trunk-rs/trunk/releases/download/v0.22.0/trunk-x86_64-unknown-linux-gnu.tar.gz \
  --output "$trunk_download_dir/trunk.tar.gz"
printf '%s  %s\n' f78c1989240c56592d8cfc4df5cc27399a3ff66a3f9cc819e16f1e3d809ce7c2 "$trunk_download_dir/trunk.tar.gz" | sha256sum --check
mkdir -p "$trunk_destination"
tar -xzf "$trunk_download_dir/trunk.tar.gz" -C "$trunk_destination" trunk
"$trunk_destination/trunk" --version
