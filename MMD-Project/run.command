#!/bin/bash
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" server.py --network "$@"
fi
exec python3 server.py --network "$@"
