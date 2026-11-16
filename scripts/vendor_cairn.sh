#!/usr/bin/env bash
# 重新拉取固定 commit 的上游源码到 cairn/（可重复、可校验）。
set -euo pipefail
SHA=8e7e0ea67552383851dfcabfba0c4e9c8d007878
TARBALL_SHA256=08f6cf8e573a753f97f6b5f74d4bdf930bfb92427808af3fce800e04211a8e5a
DEST="$(cd "$(dirname "$0")/.." && pwd)/cairn"
TMP="$(mktemp -d)"

python3 - "$SHA" "$TMP" <<'PY'
import sys, urllib.request
sha, tmp = sys.argv[1], sys.argv[2]
url = "https://codeload.github.com/oritera/Cairn/tar.gz/" + sha
urllib.request.urlretrieve(url, tmp + "/cairn.tar.gz")
print("downloaded", url)
PY

echo "$TARBALL_SHA256  $TMP/cairn.tar.gz" | shasum -a 256 -c -
tar -xzf "$TMP/cairn.tar.gz" -C "$TMP"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$TMP/Cairn-$SHA/." "$DEST/"
echo "vendored Cairn $SHA -> $DEST"
