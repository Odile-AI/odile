#!/bin/bash
# One UTC line into the tenant's bench log. This is the body of the
# `bench-heartbeat` Hermes cron job: every minute with --no-agent, so it costs
# no inference. Its last line is the job's stored output.
set -euo pipefail

LOG=${ODILE_BENCH_LOG:-$HOME/bench/heartbeat.log}
mkdir -p -- "$(dirname -- "$LOG")"
printf '%s bench-heartbeat fired\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG"
tail -n 1 -- "$LOG"
