#!/usr/bin/env bash
# Encrypted backup of everything not in git. Usage: scripts/backup.sh [out-dir]
set -euo pipefail
cd "$(dirname "$0")/.."

out_dir=${1:-$HOME}
stamp=$(date +%Y%m%d)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/budget-backup-$stamp"
root="$stage/budget-backup-$stamp"

if [ -z "${ACTUAL_PASSWORD:-}" ]; then
  read -rsp "Actual password: " ACTUAL_PASSWORD && echo
  export ACTUAL_PASSWORD
fi
uv run budget export "$root/actual-export.zip"

cp -a "$HOME/.config/nebula" "$root/nebula"
mkdir -p "$root/repo/.data"
cp -a .env "$root/repo/"
cp -a .data/simplefin.url "$root/repo/.data/"
for p in statements checks "Debt view.xlsx"; do
  [ -e "$p" ] && cp -a "$p" "$root/repo/"
done

(cd "$stage" && zip -qr "budget-backup-$stamp.zip" "budget-backup-$stamp")
gpg --symmetric --cipher-algo AES256 -o "$out_dir/budget-backup-$stamp.zip.gpg" "$stage/budget-backup-$stamp.zip"
echo "wrote $out_dir/budget-backup-$stamp.zip.gpg"
