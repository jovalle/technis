"""Verify live targets, dashboard queries and rule evaluation through Nexus SSH.

Uses only the private backend network. Saves query outcomes without credentials.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def panels(items):
    for panel in items:
        yield panel
        yield from panels(panel.get("panels", []))


def queries(host):
    result = []
    for path in (ROOT / "docker/services/grafana/files/dashboards").glob("*.json"):
        if path.name == "fleet-overview.json":
            continue
        dashboard = json.loads(path.read_text())
        for panel in panels(dashboard["panels"]):
            for target in panel.get("targets", []):
                if "expr" not in target:
                    continue
                expression = target["expr"]
                for variable in dashboard.get("templating", {}).get("list", []):
                    if variable["name"] in ("host", "group", "cgroup", "signal"): continue
                    value = variable.get("allValue", variable.get("query") if variable.get("type")=="constant" else ".*")
                    expression = expression.replace("${"+variable["name"]+"}",str(value)).replace("$"+variable["name"],str(value))
                for name, value in {"__rate_interval": "1m", "__range": "1h", "__interval": "30s",
                                    "host": host, "group": ".*", "cgroup": ".*", "signal": "cpu_busy_ratio"}.items():
                    expression = expression.replace("${"+name+"}",value).replace("$"+name,value)
                result.append({"dashboard":dashboard["uid"], "panel":panel["title"],"host":host,"query":expression})
    return result


REMOTE = '''
import base64, concurrent.futures, json, subprocess, urllib.request, urllib.parse
def inspect(name):
 return json.loads(subprocess.check_output(['docker','inspect',name]))[0]
def address(name,port):
 return 'http://'+next(v['IPAddress'] for v in inspect(name)['NetworkSettings']['Networks'].values() if v['IPAddress'])+':'+str(port)
vm=address('victoriametrics',8428)
def query(row):
 try:
  data=json.load(urllib.request.urlopen(vm+'/api/v1/query?'+urllib.parse.urlencode({'query':row['query']}),timeout=30))
  row.update(status=data['status'],series=len(data.get('data',{}).get('result',[])))
  if row['dashboard']=='health': row['results']=data.get('data',{}).get('result',[])
 except Exception as e: row.update(status='error',error=str(e))
 return row
rows=json.loads(base64.b64decode(PAYLOAD))
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 results=list(pool.map(query,rows))
rules=json.load(urllib.request.urlopen(address('vmalert-metrics',8880)+'/api/v1/rules'))
groups=rules['data']['groups']
print(json.dumps({'queries':results,'rule_groups':len(groups),'rules':[{'name':r['name'],'health':r.get('health'),'lastError':r.get('lastError','')} for g in groups for r in g['rules']]}))
'''


if __name__ == "__main__":
    import base64
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/technis-observability/verification.json")
    args = parser.parse_args()
    probe = [q for host in ("stargate","nexus","mothership") for q in queries(host)] + [{"dashboard":"health", "panel":"targets", "query":'up{job=~"node|process|host-observer"}'},
                          {"dashboard":"health", "panel":"supplemental failures", "query":'technis_collector_success == 0'},
                          {"dashboard":"health", "panel":"textfile errors", "query":'node_textfile_scrape_error{job="node"} > 0'},
                          {"dashboard":"health", "panel":"hardware unavailable", "query":'technis_expected_exporter unless on(host,exporter) label_replace(up{job=~"smartctl|drm|nvidia"} == 1,"exporter","$1","job","(.*)")'},
                          {"dashboard":"health", "panel":"owner read errors", "query":'technis_owner_read_errors > 0'}]
    code = REMOTE.replace("PAYLOAD",repr(base64.b64encode(json.dumps(probe).encode()).decode()))
    result = subprocess.run(["ssh","nexus","python3 -"],input=code,text=True,capture_output=True,check=True)
    output = json.loads(result.stdout)
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(output,indent=2)+"\n")
    failed=[q for q in output['queries'] if q['status']!='success']
    empty=[q for q in output['queries'] if q.get('series')==0]
    rule_errors=[r for r in output['rules'] if r['lastError'] or r['health']=='err']
    print(f"{len(output['queries'])} queries; {len(failed)} query errors; {len(empty)} empty results; {len(output['rules'])} rules; {len(rule_errors)} rule errors")
    for q in failed: print(q['dashboard'],q['panel'],q['error'])
    for r in rule_errors: print(r)
    health = {q['panel']: q for q in output['queries'] if q['dashboard']=='health'}
    targets = {(r['metric'].get('host'),r['metric'].get('job')) for r in health['targets'].get('results',[]) if float(r['value'][1]) == 1}
    expected = {(h,j) for h in ('stargate','nexus','mothership') for j in ('node','process','host-observer')}
    health_errors = sorted(expected-targets)
    for panel in ('supplemental failures','textfile errors','hardware unavailable','owner read errors'):
        if health[panel].get('series',0): health_errors.append(panel)
    print('Collector health:', 'PASS' if not health_errors else health_errors)
    if failed or rule_errors or health_errors: raise SystemExit(1)
