#!/bin/bash
# Verify the progress bar is monotonic and the denominator is stable across
# fragments. Queues a merged (video+audio) download, which is the case that
# exposed the per-fragment total wobble.
P=8090
B=http://127.0.0.1:$P
VID="https://www.youtube.com/watch?v=aqz-KE-bpKQ"

# Clean slate so this test is unambiguous.
curl -s -X POST "$B/api/clean" -H 'Content-Type: application/json' -d '{"force":true}' >/dev/null

echo "=== queue 360p merged video+audio ==="
curl -s --max-time 200 -X POST "$B/api/scan" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$VID\"}" > /tmp/s.json
FMT=$(python3 -c "
import json; d=json.load(open('/tmp/s.json'))
vs=[v for v in d['result']['video'] if v['height']==360]
print(vs[0]['format_id'] if vs else 'bv*[height<=360]+ba')")
echo "format: $FMT"

# needs_audio=True for this format, so the worker must merge a second stream.
curl -s --max-time 30 -X POST "$B/api/download" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$VID\",\"video_id\":\"aqz-KE-bpKQ\",\"title\":\"ProgressTest\",\"kind\":\"video\",\"quality\":\"360p\",\"format_id\":\"$FMT\"}"
echo; echo

LAST=0
for i in $(seq 1 100); do
  R=$(curl -s --max-time 15 "$B/api/jobs" | python3 -c "
import json,sys
j=[x for x in json.load(sys.stdin)][0]
print('%s|%s|%.2f|%d|%d|%s' % (j['status'], j['phase'], j['percent'] or 0,
      j['downloaded_bytes'] or 0, j['total_bytes'] or 0, j['message'][:60]))")
  ST=$(echo "$R" | cut -d'|' -f1); PCT=$(echo "$R" | cut -d'|' -f3)
  DN=$(echo "$R" | cut -d'|' -f4); TT=$(echo "$R" | cut -d'|' -f5)
  echo "  [$i] $R"
  # monotonicity check on downloaded bytes
  if [ "$DN" -lt "$LAST" ] 2>/dev/null; then
    echo "  !! REGRESSION: bytes went $LAST -> $DN"
  fi
  LAST=$DN
  case "$ST" in done|error) break;; esac
  sleep 4
done

echo
echo "=== final ==="
curl -s --max-time 20 "$B/api/jobs" | python3 -c "
import json,sys
j=json.load(sys.stdin)[0]
print('status  :', j['status'])
print('percent :', j['percent'])
print('bytes   :', j['downloaded_human'], '/', j['total_human'])
print('message :', j['message'])
print('result  :', j['result_path'])"
echo
echo "=== library ==="
curl -s --max-time 20 "$B/api/library" | python3 -c "
import json,sys
for m in json.load(sys.stdin)['items']:
    print(' ', m['title'][:30], '|', m['kind'], '|', m['bytes_human'],
          '| thumb:', m['thumb'], '| expires_in:', m['expires_in_days'], 'd')"
rm -f /tmp/s.json