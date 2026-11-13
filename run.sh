#!/bin/bash
# Wrapper — forward ke scripts/run-server.sh
# Usage: ./run.sh [start|stop|restart|status|logs]

set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec ./scripts/run-server.sh "$@"
