#!/usr/bin/env bash
# check-wsl.sh -- run INSIDE WSL to verify the container <-> Windows bot link.
#
#   cd /mnt/d/Code/JungJingNew/deploy/snowluma
#   bash check-wsl.sh
#
# Exit code 0 = everything it could check looks fine.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

PORT=8085
if [ -f .env ]; then
  # shellcheck disable=SC1091
  PORT="$(grep -E '^REVERSE_WS_PORT=' .env | tail -n1 | cut -d= -f2 | tr -d '[:space:]')"
  PORT="${PORT:-8085}"
fi

ok()   { printf '    OK   %s\n' "$*"; }
warn() { printf '    WARN %s\n' "$*"; }
bad()  { printf '    FAIL %s\n' "$*"; }
step() { printf '==> %s\n' "$*"; }

FAILED=0

step "Docker in WSL"
if command -v docker >/dev/null 2>&1; then
  DVER="$(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  if [ -n "$DVER" ]; then
    ok "docker server $DVER"
  else
    bad "docker CLI present but daemon unreachable (sudo service docker start?)"
    FAILED=1
  fi
else
  bad "docker not installed in this distro"
  FAILED=1
fi

if docker compose version >/dev/null 2>&1; then
  ok "docker compose $(docker compose version --short 2>/dev/null)"
else
  bad "docker compose plugin missing"
  FAILED=1
fi

step "Container state"
STATE="$(docker inspect -f '{{.State.Status}}' snowluma 2>/dev/null || true)"
if [ "$STATE" = "running" ]; then
  ok "snowluma is running"
else
  bad "snowluma state: ${STATE:-not found}"
  FAILED=1
fi

step "Container -> Windows run.py (tcp ${PORT})"
if [ "$STATE" = "running" ]; then
  RESULT="$(docker exec snowluma node -e "
    const net=require('net');
    const s=net.connect(${PORT},'host.docker.internal');
    s.setTimeout(3000);
    s.on('connect',()=>{console.log('CONNECT_OK');s.destroy();process.exit(0);});
    s.on('timeout',()=>{console.log('TIMEOUT');s.destroy();process.exit(1);});
    s.on('error',e=>{console.log('ERROR:'+e.code);process.exit(1);});
  " 2>&1)"
  case "$RESULT" in
    *CONNECT_OK*) ok "host.docker.internal:${PORT} reachable - run.py is listening" ;;
    *TIMEOUT*)    bad "timeout: firewall is dropping packets (run setup-windows.ps1 as admin)"; FAILED=1 ;;
    *ECONNREFUSED*) bad "connection refused: nothing listening on Windows ${PORT} (is run.py started? ws_reverse_host=0.0.0.0?)"; FAILED=1 ;;
    *ENOTFOUND*|*EAI_AGAIN*) bad "host.docker.internal not resolvable (add extra_hosts: host-gateway, or use the WSL/Windows IP)"; FAILED=1 ;;
    *)            bad "unexpected: $RESULT"; FAILED=1 ;;
  esac
else
  warn "container not running, skipped"
fi

step "OneBot reverse client registered in SnowLuma?"
if [ "$STATE" = "running" ]; then
  if docker exec snowluma sh -lc 'ls /app/data/config/onebot*.json' >/dev/null 2>&1; then
    docker exec snowluma sh -lc '
      for f in /app/data/config/onebot*.json; do
        echo "--- $f"
        node -e "
          const fs=require(\"fs\");
          const j=JSON.parse(fs.readFileSync(process.argv[1],\"utf8\"));
          const c=(j.networks&&j.networks.wsClients)||[];
          if(!c.length){console.log(\"  (no wsClients - add one in WebUI)\");}
          for(const x of c){console.log(\"  \"+(x.enabled===false?\"[off] \":\"[on]  \")+(x.name||\"?\")+\" -> \"+(x.url||\"?\")+\" role=\"+(x.role||\"\")+\" token=\"+((x.accessToken||\"\").length?\"set\":\"EMPTY\"));}
        " "$f"
      done'
  else
    warn "onebot.json not generated yet (log in to QQ first)"
  fi
else
  warn "container not running, skipped"
fi

step "Recent connection log lines"
if [ "$STATE" = "running" ]; then
  docker logs --tail 200 snowluma 2>&1 | grep -Ei 'onebot|ws-client|connected|disconnect' | tail -n 10 || warn "no OneBot log lines yet"
else
  warn "container not running, skipped"
fi

step "Access info"
docker logs snowluma 2>&1 | grep -E '远程桌面密码|remote desktop password' | tail -n 1 || warn "no VNC password line"
docker logs snowluma 2>&1 | grep -E '临时密码|initial credentials' | tail -n 1 || warn "no WebUI temp password line (only printed on a fresh data volume)"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "Looks good. Make sure SnowLuma WebUI has: url=ws://host.docker.internal:${PORT}/ , role=Universal , accessToken=<same as config/config.json>"
else
  echo "Something failed above - fix the FAIL lines first."
fi
exit "$FAILED"
