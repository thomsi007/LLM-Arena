#!/usr/bin/env sh
# Start LLM Arena on http://127.0.0.1:8765
cd "$(dirname "$0")" && exec python3 -m llm_arena --open "$@"
