"""Check screen geometry, bounded lists, and optional live queries for the NOC."""
import base64
import json
from pathlib import Path
import subprocess
import sys
from noc import generate, app
import re
from verify import REMOTE

d = generate()
rects = [p['gridPos'] for p in d['panels']]
for i,a in enumerate(rects):
    assert a['x'] >= 0 and a['y'] >= 0 and a['x']+a['w'] <= 24 and a['y']+a['h'] <= 26
    for b in rects[i+1:]:
        assert a['x']+a['w']<=b['x'] or b['x']+b['w']<=a['x'] or a['y']+a['h']<=b['y'] or b['y']+b['h']<=a['y'], (a,b)
assert all(p['type'] in ('stat','table','timeseries','text') for p in d['panels'])
assert all('repeat' not in p for p in d['panels'])
for p in d['panels']:
    if p['title'].startswith(('ACTIVE PROBLEMS','RECENT EVENTS')):
        assert 'topk(5,' in p['targets'][0]['expr']
assert 26*30 + 25*8 + 32 <= 1080
print('PASS: fixed 1920×1080 grid budget, no overlapping panels, bounded exception/event lists, native plugins')
if '--live' in sys.argv:
    substitutions={v['name']:v.get('allValue',v.get('query')) for v in d['templating']['list']}
    rows=[]
    for p in d['panels']:
        for t in p['targets']:
            q=t['expr']
            for name,value in substitutions.items():q=q.replace('$'+name,str(value))
            rows.append({'dashboard':'noc','panel':p['title'],'query':q})
    # Evaluate the actual application-state expression against synthetic vectors.
    # This never writes telemetry and detects false-green/no-health-check regressions.
    cases=[(1,1,0,0,0),(1,0,0,0,1),(1,0,1,0,3),(1,0,0,1,2),(0,0,0,0,3)]
    for i,(running,healthy,unhealthy,starting,expected) in enumerate(cases):
        expression=app('fixture')
        def replace_metric(match):
            token=match.group(0)
            if 'timestamp_seconds' in token:return 'vector(time())'
            if '_running' in token:return f'vector({running})'
            value=healthy if 'health="healthy"' in token else unhealthy if 'health="unhealthy"' in token else starting
            return f'vector({value})'
        expression=re.sub(r'technis_[a-z_]+\{[^}]+\}',replace_metric,expression)
        rows.append({'dashboard':'health','panel':f'state_case_{i}','query':expression,'expected':expected})
    code=REMOTE.replace('PAYLOAD',repr(base64.b64encode(json.dumps(rows).encode()).decode()))
    result=subprocess.run(['ssh','nexus','python3 -'],input=code,text=True,capture_output=True,check=True)
    data=json.loads(result.stdout)
    Path('/tmp/technis-observability/noc-verification.json').write_text(json.dumps(data,indent=2)+'\n')
    failed=[r for r in data['queries'] if r['status']!='success']
    for r in failed: print(r['panel'],r['error'])
    assert not failed
    for result in data['queries']:
        if 'expected' in result:
            assert len(result.get('results',[]))==1 and float(result['results'][0]['value'][1])==result['expected'], result
    print(f'PASS: {len(rows)} live queries; {sum(r["series"]==0 for r in data["queries"])} empty results (inspect coverage)')
