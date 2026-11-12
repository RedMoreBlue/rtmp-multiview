#!/usr/bin/env bash
# Smoke test of the Docker image: RTMP source -> multiview container -> login, live status,
# MJPEG stream, security checks. Usage: ci/smoke-test.sh <image>
set -euo pipefail
IMG="${1:-rtmp-multiview:test}"
NET=mvtest-$$
CFG=$(mktemp -d)
KEY=SECRETKEY123
PW=ci-test-pass-123
BASE=http://127.0.0.1:18080
JAR=$(mktemp)
fails=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
cleanup() {
  docker logs mvapp-$$ > "$CFG/app.log" 2>&1 || true
  docker rm -f mvsrc-$$ mvapp-$$ >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. refuses to start without a password
if timeout 20 docker run --rm "$IMG" > "$CFG/nopw.log" 2>&1; then
  fail "container started without a password"
elif grep -q "No web password" "$CFG/nopw.log"; then
  pass "refuses to start without a password"
else
  fail "unexpected start-up failure without password: $(tail -3 "$CFG/nopw.log")"
fi

# 2. hash helper works non-interactively? (needs a tty: just check it is wired)
docker run --rm "$IMG" --help | grep -q -- "--hash-password" && pass "--hash-password option present" || fail "--hash-password missing"

# 3. RTMP test source + multiview
docker network create "$NET" >/dev/null
docker run -d --name mvsrc-$$ --network "$NET" --network-alias mvsrc --entrypoint ffmpeg "$IMG" \
  -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=1280x720:rate=25 \
  -f lavfi -i sine=frequency=700:sample_rate=48000 -c:v libx264 -preset ultrafast -tune zerolatency \
  -g 50 -c:a aac -ac 2 -f flv -listen 1 "rtmp://0.0.0.0:1935/live/$KEY" >/dev/null
cat > "$CFG/config.json" <<EOF
{"inputs": [{"name": "CI live", "url": "rtmp://mvsrc:1935/live/$KEY"},
            {"name": "CI refused", "url": "rtmp://mvsrc:1936/live/$KEY"},
            {"name": "Three", "url": ""}, {"name": "Four", "url": ""}]}
EOF
chmod -R a+rwX "$CFG"
docker run -d --name mvapp-$$ --network "$NET" -p 18080:8080 -e MV_PASSWORD="$PW" \
  --health-interval=3s -v "$CFG:/config" "$IMG" >/dev/null

for i in $(seq 1 40); do curl -fs "$BASE/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "$BASE/healthz" | grep -q ok && pass "healthz" || fail "healthz"
[ "$(docker exec mvapp-$$ id -u)" = "10001" ] && pass "runs as non-root (uid 10001)" || fail "not running as uid 10001"

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/status")
[ "$code" = "401" ] && pass "status needs login" || fail "status without login gave $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/stream.mjpg?p=mobile")
[ "$code" = "401" ] && pass "stream needs login" || fail "stream without login gave $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "{\"password\":\"$PW\"}" "$BASE/api/login")
[ "$code" = "403" ] && pass "login without X-MV header refused" || fail "CSRF check gave $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-MV: 1' -H 'Content-Type: application/json' -d '{"password":"wrong"}' "$BASE/api/login")
[ "$code" = "401" ] && pass "wrong password refused" || fail "wrong password gave $code"
code=$(curl -s -c "$JAR" -o /dev/null -w '%{http_code}' -H 'X-MV: 1' -H 'Content-Type: application/json' -d "{\"password\":\"$PW\"}" "$BASE/api/login")
[ "$code" = "200" ] && pass "login" || fail "login gave $code"

state=""
for i in $(seq 1 40); do
  state=$(curl -s -b "$JAR" "$BASE/api/status" | python3 -c 'import sys,json; print(json.load(sys.stdin)["inputs"][0]["state"])' 2>/dev/null || true)
  [ "$state" = "live" ] && break
  sleep 1
done
[ "$state" = "live" ] && pass "RTMP input goes LIVE" || fail "input state is '$state'"
st=$(curl -s -b "$JAR" "$BASE/api/status")
echo "$st" | python3 -c 'import sys,json; i=json.load(sys.stdin)["inputs"]; print("   ", i[0]["info"], "|", i[0]["audio"], "|", i[0]["fps"], "fps |", i[1]["state"], i[1]["error"])'
echo "$st" | grep -q "$KEY" && fail "stream key visible in status" || pass "stream key not in status"

curl -s -b "$JAR" --max-time 6 "$BASE/stream.mjpg?p=mobile" -o "$CFG/stream.bin" || true
n=$(python3 -c 'import sys; print(open(sys.argv[1],"rb").read().count(b"\xff\xd9"))' "$CFG/stream.bin")
[ "$n" -ge 10 ] && pass "MJPEG stream delivered $n frames in 6 s" || fail "MJPEG stream delivered only $n frames"

cfgcode=$(curl -s -b "$JAR" -o "$CFG/cfg.json" -w '%{http_code}' "$BASE/api/config")
[ "$cfgcode" = "200" ] && grep -q "$KEY" "$CFG/cfg.json" && pass "settings readable after login (single password)" || fail "api/config gave $cfgcode"

for i in $(seq 1 20); do
  h=$(docker inspect -f '{{.State.Health.Status}}' mvapp-$$)
  [ "$h" = "healthy" ] && break
  sleep 2
done
[ "$h" = "healthy" ] && pass "Docker healthcheck healthy" || fail "health status $h"

docker logs mvapp-$$ 2>&1 | grep -q "$KEY" && fail "stream key appears in the logs" || pass "stream key not in logs"

echo
if [ "$fails" -eq 0 ]; then echo "All smoke tests passed"; else
  echo "$fails smoke test(s) failed"; echo "--- app log"; docker logs mvapp-$$ 2>&1 | tail -40; exit 1; fi
