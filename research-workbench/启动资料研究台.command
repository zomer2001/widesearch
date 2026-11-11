#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
(
  for i in $(seq 1 90); do
    if /usr/bin/curl --silent --fail --max-time 1 http://127.0.0.1:3000/api/workbench/settings >/dev/null; then
      /usr/bin/open http://127.0.0.1:3000/
      exit 0
    fi
    sleep 1
  done
) &
exec /bin/bash ./web/start.sh
