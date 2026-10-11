#!/usr/bin/env bash
# Usage: bash travel/start_agents.sh weather flights hotels budget
set -u
cd "$(dirname "$0")/.."                      # run from the lungo folder
set -a; source .env; set +a                  # LLM key, SLIM secret
export PYTHONPATH=. SLIM_SERVER=localhost:46357
pkill -f "travel.run" 2>/dev/null; sleep 3
if pgrep -f "travel.run" >/dev/null; then echo "old agents still running - stop them first"; exit 1; fi
for a in "$@"; do
  nohup uv run python -m travel.run "$a" > "/tmp/agent_$a.log" 2>&1 &
done
for i in $(seq 1 30); do                       # wait up to 30 s for all to be ready
  ready=$(grep -l "ready" $(printf "/tmp/agent_%s.log " "$@") 2>/dev/null | wc -l)
  [ "$ready" -eq "$#" ] && break; sleep 1
done
echo "$ready/$# agents ready"
for a in "$@"; do grep -q ready "/tmp/agent_$a.log" || { echo "--- $a failed:"; tail -5 "/tmp/agent_$a.log"; }; done