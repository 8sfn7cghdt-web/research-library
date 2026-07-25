#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist="$root/dist"

rm -rf "$dist"
mkdir -p "$dist/server"

cp -R "$root/docs"/. "$dist"/
cp "$root/scripts/sites-static-server.js" "$dist/server/index.js"
