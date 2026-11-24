#!/usr/bin/env bash
# Starts a fresh chopsticks fork of Asset Hub Polkadot on ws://127.0.0.1:8000.
# Needs Node.js 22 or newer. Stops a fork started earlier by this script.
set -euo pipefail
cd "$(dirname "$0")/chopsticks"

major=$(node -p 'process.versions.node.split(".")[0]')
if [ "$major" -lt 22 ]; then
  echo "Node.js $major found; chopsticks needs 22 or newer (nvm use 22)" >&2
  exit 1
fi
[ -d node_modules ] || npm install --no-audit --no-fund

pkill -f "chopsticks --config ahp.yml" 2>/dev/null || true
sleep 1
rm -f db.sqlite*   # the fork starts from the latest block every time
npx chopsticks --config ahp.yml > fork.log 2>&1 &
for _ in $(seq 1 180); do
  if grep -q "listening" fork.log; then
    echo "fork ready on ws://127.0.0.1:8000 (log: chopsticks/fork.log, stop: pkill -f chopsticks)"
    exit 0
  fi
  sleep 1
done
echo "fork did not start; see chopsticks/fork.log" >&2
exit 1
