#!/bin/bash
# End-to-end functional test of the LocalYTManager API.
P=8090
B=http://127.0.0.1:$P
VID="https://www.youtube.com/watch?v=aqz-KE-bpKQ"   # Big Buck Bunny, CC, short

echo "=== 1. SCAN (audio+video options) ==="
curl -s --max-time 200 -X POST "$B/api/scan" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$VID\"}" > /tmp/t_scan.json
python3 - <<'EOF'
import json
d=json.load(open('/tmp/t_scan.json'))
if not d.get('ok'): print('FAIL:', d); raise SystemExit(1)
r=d['result']
print('title     :', r['title'][:60])
print('video_id  :', r['video_id'], '| duration:', r['duration'], '| thumb:', bool(r['thumbnail']))
print('AUDIO options:')
for a in r['audio'][:4]: print('   ', a['label'], '| fid', a['format_id'])
print('VIDEO options:')
for v in r['video'][:6]: print('   ', v['label'], '| fid', v['format_id'], '| needs_audio', v['needs_audio'])
EOF

echo
echo "=== 2. QUEUE a small video download ==="
# pick the smallest video option so the test is quick
VIDQ=$(python3 -c "
import json; d=json.load(open('/tmp/t_scan.json'))
vs=[v for v in d['result']['video'] if v['height']<=360]
vs.sort(key=lambda v: v.get('size') or 0)
print(vs[0]['format_id'] if vs else d['result']['video'][0]['format_id'])")
echo "using format: $VIDQ"
curl -s --max-time 30 -X POST "$B/api/download" -H 'Content-Type: application/json' \
  -d "{\"url\":\"$VID\",\"video_id\":\"aqz-KE-bpKQ\",\"title\":\"TEST video\",\"kind\":\"video\",\"quality\":\"test\",\"format_id\":\"$VIDQ\"}" \
  | tee /tmp/t_job.json
echo

echo "=== 3. WATCH PROGRESS (proves background + real byte counts) ==="
for i in $(seq 1 60); do
  S=$(curl -s --max-time 15 "$B/api/jobs" | python3 -c "
import json,sys
j=json.load(sys.stdin)[0]
print('%s|%s|%.1f|%s|%s' % (j['status'], j['phase'], j['percent'] or 0, j['downloaded_human'], j['total_human']))")
  echo "  [$i] $S"
  case "$S" in done*|error*) break;; esac
  sleep 5
done

echo
echo "=== 4. LIBRARY ==="
curl -s --max-time 20 "$B/api/library" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('items:', len(d['items']))
for m in d['items']:
    print('  %-22s %-5s %-10s %8s  thumb=%s  expires_in=%sd  exists=%s'
          % (m['title'][:22], m['kind'], m['quality'] or '-', m['bytes_human'],
             bool(m['thumb']), m['expires_in_days'], m['exists']))
    print('  rel_path:', m['rel_path'])
"
rm -f /tmp/t_scan.json /tmp/t_job.json