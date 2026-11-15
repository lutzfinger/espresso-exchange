#!/bin/sh
# Every inline <script> of every page must parse. Prints nothing and exits 0 when all parse.
cd "$(dirname "$0")/.." || exit 1
tmp=$(mktemp -d)
status=0
for f in server/static/*.html; do
  python3 - "$f" "$tmp/$(basename "$f").js" <<'PY'
import re, sys
html = open(sys.argv[1]).read()
open(sys.argv[2], "w").write("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
PY
  node --check "$tmp/$(basename "$f").js" || { echo "script error in $f"; status=1; }
done
rm -rf "$tmp"
exit $status
