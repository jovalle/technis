"""Build the fixed-screen operations console using native Grafana panels only.

Adapters below are the only site-specific query definitions. No collector or alert
configuration is changed. Missing evidence is UNKNOWN, never synthetic success.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docker/services/grafana/files/dashboards/noc.json'
DS = {'type': 'prometheus', 'uid': 'victoriametrics'}
# Current labels: stack identifies the host stack; compose_project is not exported.
S = 'environment=~"$environment",host=~"$host",stack=~"$stack"'
N = S + ',job="node"'
EXCLUDE = 'container!~"$ignored_containers"'
GOOD, UNKNOWN, WARN, BAD = '#719580', '#9299A5', '#FFB547', '#F2495C'
BLUE = '#83A9CF'
MAP = [{'type':'value','options':{str(i):{'text':t,'color':c,'index':i} for i,t,c in
       [(0,'OK',GOOD),(1,'UNKNOWN',UNKNOWN),(2,'WARN',WARN),(3,'FAIL',BAD)]}},
       {'type':'special','options':{'match':'null','result':{'text':'UNKNOWN','color':UNKNOWN}}}]


def m(name, extra='', scope=S):
    return name+'{'+scope+(','+extra if extra else '')+'}'


def maximum(expressions):
    return 'max('+ ' or '.join('label_replace(('+q+'),"check","'+str(i)+'",""," ")'.replace('" "','""') for i,q in enumerate(expressions))+')'


def fresh(query, source='technis_collector_timestamp_seconds', seconds=45):
    stamp = m(source) if source=='technis_collector_timestamp_seconds' else 'timestamp('+source+')'
    return '('+query+') and on(host) (time() - '+stamp+' < '+str(seconds)+')'


def bad_if(expr, severity=3):
    return '('+str(severity)+' * ('+expr+')) or vector(1)'


def probe(regex):
    v=m('gatus_results_endpoint_success','name=~"'+regex+'"')
    return '(3 * (1 - min(('+v+' and (time()-timestamp('+v+') < 90)) and on(host) ('+m('up','job="gatus"')+' == 1)))) or vector(1)'


def app(regex):
    sel='container=~"'+regex+'",'+EXCLUDE
    running=m('technis_docker_container_running',sel)
    def health(state):
        return 'max by(host,container) ('+m('technis_docker_container_health',sel+',health="'+state+'"')+')'
    # Running without a health check is unknown. Starting is warning; stopped/unhealthy critical.
    q='clamp_max(3*(1-'+running+') + on(host,container) (1 - '+health('healthy')+' + 2*'+health('unhealthy')+' + '+health('starting')+'),3)'
    return 'max('+fresh(q)+') or vector(1)'


def scrape(regex):
    v=m('up','job=~"'+regex+'"')
    return '(3*(1-min('+v+' and (time()-timestamp('+v+') < 90)))) or vector(1)'


RUN=m('technis_docker_container_running',EXCLUDE)
HC=m('technis_docker_container_health',EXCLUDE+',health="healthy"')
UH=m('technis_docker_container_health',EXCLUDE+',health="unhealthy"')
CPU='1-avg by(host)(rate('+m('node_cpu_seconds_total','job="node",mode="idle"')+'[1m]))'
MEM='max by(host)(1-'+m('node_memory_MemAvailable_bytes',scope=N)+'/'+m('node_memory_MemTotal_bytes',scope=N)+')'
FS='max by(host)(1-'+m('node_filesystem_avail_bytes','fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"',N)+'/'+m('node_filesystem_size_bytes','fstype!~"tmpfs|overlay|squashfs",mountpoint!~"/run.*|/var/lib/docker.*"',N)+')'
AL=m('ALERTS','alertstate="firing"')
CRIT='count('+m('ALERTS','alertstate="firing",priority=~"P0|P1"')+') or vector(0)'
WARNING='count('+m('ALERTS','alertstate="firing",priority!~"P0|P1"')+') or vector(0)'
HOST_OK='sum('+m('up','job="node"')+' == 1) or vector(0)'
SMART='(3*(1-min('+m('smartctl_device_smart_status')+'))) or vector(1)'
WAN=probe('$wan_probe')
PRESSURE='max by(host)(avg_over_time('+m("technis_pressure_average_ratio",'resource="cpu",scope="some",window="10"',N)+'[5m]))'
HOST_STATE='clamp_max(3*(1-max by(host)('+m('up','job="node"')+')) + 2*('+CPU+'> bool 0.90) + 2*('+MEM+'> bool 0.90) + 2*('+FS+'> bool 0.90) + 2*('+PRESSURE+'> bool 0.20),3)'
CONTAINER_BAD='sum by(host)(('+RUN+' == bool 0) + on(host,container) max by(host,container)('+UH+'))'
HOST_STATE='clamp_max(('+HOST_STATE+') + on(host) ((3*max by(host)('+m('ALERTS','alertstate="firing",priority=~"P0|P1"')+')) or (0*max by(host)('+m('up','job="node"')+'))),3)'
ALIVE='max(time()-'+m('technis_collector_timestamp_seconds')+')'

DOMAINS = [
 ('NETWORK',[('DNS',app('.*technitium')),('Proxy',app('.*traefik')),('VPN',app('tailscale|gluetun'))]),
 ('COMPUTE / HOSTS',[(h,'max('+HOST_STATE.replace('host=~"$host"','host="'+h+'"')+') or vector(1)') for h in ['stargate','nexus','mothership']]),
 ('CONTAINERS',[('Checks',bad_if('max('+UH+') > bool 0')),('Stopped',bad_if('max(1-'+RUN+') > bool 0')),('OOM / 15m',bad_if('sum(increase('+m('container_oom_events_total','name!=""')+'[15m])) > bool 0',2))]),
 ('STORAGE',[('SMART',SMART),('Capacity',bad_if('max('+FS+') > bool 0.85',2)),('Alerts',bad_if('(count('+m('ALERTS','alertstate="firing",service="storage"')+') or vector(0)) > bool 0') )]),
 ('CORE INFRA',[('Auth',probe('Authentication')),('Postgres',bad_if('1-min('+m('pg_up')+')')),('Redis',bad_if('1-min('+m('redis_up')+')'))]),
 ('OBSERVABILITY',[('Metrics',scrape('victoriametrics')),('Logs',scrape('victorialogs')),('Collectors',scrape('node|process|host-observer'))]),
 ('USER SERVICES',[('Media',probe('Plex|Jellyfin|Navidrome')),('Photos',probe('Immich')),('Files',probe('Nextcloud'))]),
 ('BACKUPS',[('Primary','vector(1)'),('Offsite','vector(1)'),('Last run','vector(1)')]),
 ('EXTERNAL',[('Cloudflare',probe('Cloudflare')),('Tunnels',scrape('cloudflared.*')),('WAN',WAN)]),
]
# Monitoring incompleteness cannot mask a critical state, and prevents all-clear.
OVERALL=maximum([maximum([q for _,q in checks]) for _,checks in DOMAINS]+[
 '3*(('+CRIT+') > bool 0)','2*(('+WARNING+') > bool 0)',
 '(('+HOST_OK+') < bool $expected_hosts)', '((('+ALIVE+') > bool 45) or vector(1))'])


def target(expr, ref='A', name='', table=False, history=False):
    return {'refId':ref,'expr':expr,'datasource':DS,'legendFormat':name,
            'instant':not history,'range':history,'format':'table' if table else 'time_series'}


def panel(title,x,y,w,h,kind='stat',description=''):
    p={'id':len(D['panels'])+1,'title':title,'type':kind,'datasource':DS,'gridPos':dict(x=x,y=y,w=w,h=h),
       'description':description,'targets':[],'fieldConfig':{'defaults':{'noValue':'UNKNOWN','decimals':0,'color':{'mode':'thresholds'},
       'thresholds':{'mode':'absolute','steps':[{'color':GOOD,'value':None}]},'mappings':[]},'overrides':[]}}
    D['panels'].append(p)
    return p


def stat(title,x,y,w,h,checks,state=False,unit='short',size=24):
    p=panel(title,x,y,w,h)
    p['targets']=[target(q,chr(65+i),name) for i,(name,q) in enumerate(checks)]
    p['options']={'reduceOptions':{'values':False,'calcs':['lastNotNull'],'fields':''},'orientation':'horizontal',
                  'textMode':'value_and_name' if len(checks)>1 else 'value','colorMode':'value','graphMode':'none',
                  'justifyMode':'center','wideLayout':True,'text':{'titleSize':14,'valueSize':size}}
    p['fieldConfig']['defaults']['unit']=unit
    if state:p['fieldConfig']['defaults']['mappings']=MAP
    return p


def table(title,x,y,w,h,expr,fields,description=''):
    p=panel(title,x,y,w,h,'table',description)
    p['targets']=[target(expr,table=True)]
    p['options']={'showHeader':True,'cellHeight':'sm','sortBy':[],'footer':{'show':False}}
    p['fieldConfig']['defaults']['custom']={'align':'left','minWidth':50,'cellOptions':{'type':'auto'}}
    p['transformations']=[{'id':'filterFieldsByName','options':{'include':{'names':list(fields)}}},
        {'id':'organize','options':{'indexByName':{n:i for i,n in enumerate(fields)},'renameByName':fields}}]
    return p


def label(q, name, value):
    return 'label_replace(('+q+'),"'+name+'","'+value+'","","")'


def override(p,name,props):
    p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':name},'properties':[{'id':k,'value':v} for k,v in props.items()]})


D={}
def generate():
    global D
    variables=[]
    for name,query in [('environment','label_values(up,environment)'),('host','label_values(node_uname_info,host)'),('stack','label_values(up,stack)')]:
        variables.append({'name':name,'type':'query','query':query,'datasource':DS,'refresh':1,'multi':True,'includeAll':True,'allValue':'.*','hide':2,'current':{'text':'All','value':'$__all'}})
    for name,value in [('expected_hosts','3'),('ignored_containers','.*-init|.*-data-init'),('wan_probe','WAN|Internet'),('dashboard_revision','2026-09-18')]:
        variables.append({'name':name,'type':'constant','query':value,'hide':2,'current':{'text':value,'value':value}})
    D={'uid':'technis-noc','title':'Technis · Operations','id':None,'schemaVersion':41,'version':1,'editable':False,
       'tags':['technis','noc','operations'],'timezone':'browser','refresh':'15s','time':{'from':'now-1h','to':'now'},
       'timepicker':{'hidden':True},'templating':{'list':variables},'annotations':{'list':[]},'links':[],
       'description':'1920×1080 kiosk operations console. Unknown is not healthy. States combine available health checks and probes; backups and WAN remain unknown without direct evidence. Events are sampled metric transitions, not a complete Docker event log.' ,'panels':[]}
    # Region 1: one compact, exception-led header.
    stat('HOMELAB',0,0,4,3,[('',OVERALL)],True,size=32)
    for title,x,expr,col in [('CRIT ALERTS',4,CRIT,BAD),('WARN ALERTS',6,WARNING,WARN)]:
        p=stat(title,x,0,2,3,[('',expr)],size=30)
        p['fieldConfig']['defaults']['thresholds']['steps']=[{'color':GOOD,'value':None},{'color':col,'value':1}]
    stat('HOSTS',8,0,2,3,[('Up',HOST_OK),('Expected','vector($expected_hosts)')],size=24)
    stat('CONTAINERS',10,0,3,3,[('Passed','sum('+HC+') or vector(0)'),('Seen','count('+RUN+') or vector(0)')],size=24)
    stat('HTTP CHECKS',13,0,3,3,[('Reachable','sum('+m('gatus_results_endpoint_success')+')'),('Monitored','count('+m('gatus_results_endpoint_success')+')')],size=24)
    stat('STORAGE',16,0,2,3,[('',maximum([q for _,q in DOMAINS[3][1]]))],True,size=24)
    stat('WAN',18,0,2,3,[('',WAN)],True,size=24)
    stat('LOCAL TIME',20,0,2,3,[('','vector(time()*1000)')],unit='time:HH:mm',size=24)
    p=stat('DATA AGE',22,0,2,3,[('',ALIVE)],unit='s',size=24)
    p['fieldConfig']['defaults']['thresholds']['steps']=[{'color':GOOD,'value':None},{'color':WARN,'value':30},{'color':BAD,'value':60}]
    # Region 2: domain matrix and bounded exception/event lists.
    for i,(name,checks) in enumerate(DOMAINS):
        p=stat(name,(i%3)*5,3+(i//3)*3,5,3,[('Domain',maximum([q for _,q in checks])),*checks],True,size=19)
        p['description']='Worst meaningful member state. OK = observed passing check; UNKNOWN = absent evidence or no health check. See source adapters for membership.'
    alerts='max by(priority,host,stack,service,alertname)(time()-('+m('ALERTS_FOR_STATE')+' and ignoring(alertstate) '+AL+'))'
    alerts='label_replace(label_replace(('+alerts+'),"object","$1","service","(.*)"),"reason","$1","alertname","(.*)")'
    problems=[alerts]
    critical_problems=[alerts.replace('alertstate="firing"','alertstate="firing",priority=~"P0|P1"')]
    for condition,object_label,reason,priority in [
        (RUN+' == 0','container','Stopped','P1'),
        (UH+' == 1','container','Unhealthy','P1'),
        (PRESSURE+' > 0.20','host','CPU pressure >20% / 5m','P2'),
        (MEM+' > 0.90','host','Memory >90%','P2'),
        (FS+' > 0.90','host','Filesystem >90%','P2'),
        (CPU+' > 0.90','host','CPU busy >90%','P2')]:
        q='label_replace((0*('+condition+')),"object","$1","'+object_label+'","(.*)")'
        row=label(label(q,'reason',reason),'priority',priority)
        problems.append(row)
        if priority in ('P0','P1'): critical_problems.append(row)
    problems='max by(priority,host,stack,object,reason)('+ ' or '.join(problems)+')'
    # A zero duration is deliberately displayed as a dash for derived conditions.
    # Alert ages come from the rule evaluator; no duration is invented for thresholds.
    critical_only='max by(priority,host,stack,object,reason)('+ ' or '.join(critical_problems)+')'
    score='('+problems+') + on(priority,host,stack,object,reason) (1000000000*(('+critical_only+') >= bool 0) or 0*('+problems+'))'
    expr='sort_by_label(sort_desc(('+problems+') and on(priority,host,stack,object,reason) topk(5,'+score+')),"priority")'
    p=stat('ACTIVE PROBLEMS · five highest priority',15,3,9,5,[('{{priority}} · {{host}} · {{object}} · {{reason}}',expr)],unit='s',size=16)
    p['options']['textMode']='value_and_name'
    p['targets']=[target('('+expr+') and on(priority,host,stack,object,reason) ('+critical_only+')','A','{{priority}} · {{host}} · {{object}} · {{reason}}'),
                  target('('+expr+') unless on(priority,host,stack,object,reason) ('+critical_only+')','B','{{priority}} · {{host}} · {{object}} · {{reason}}')]
    p['fieldConfig']['defaults']['mappings']=[{'type':'value','options':{'0':{'text':'—'}}}]
    p['fieldConfig']['defaults']['color']={'mode':'fixed','fixedColor':WARN}
    p['fieldConfig']['overrides'].append({'matcher':{'id':'byFrameRefID','options':'A'},'properties':[{'id':'color','value':{'mode':'fixed','fixedColor':BAD}}]})
    p['description']='Current firing alerts grouped by object/reason, plus stopped/unhealthy containers and resource thresholds. P0/P1 first, then oldest alert. A dash means condition age is unavailable. Header counts are firing alert instances, not derived-condition counts. Read-only-filesystem alerts are not suppressed.'
    events=[]
    starts='max by(host,name)('+m('container_start_time_seconds','name!=""')+')'
    events.append(label('label_replace(('+starts+'),"object","$1","name","(.*)")','event','Started'))
    events.append(label(label('max by(host)('+m('node_boot_time_seconds',scope=N)+')','object','Host'),'event','Rebooted'))
    for metric,event,extra,obj in [('container_oom_events_total','OOM detected','name!=""','name'),('technis_docker_container_running','State changed',EXCLUDE,'container'),('technis_docker_container_health','Health changed',EXCLUDE+',health="healthy"','container'),('gatus_results_endpoint_success','Probe changed','','name')]:
        series=m(metric,extra)
        q='max by(host,'+obj+')(tlast_change_over_time('+series+'[1h]) and (changes('+series+'[1h]) > 0))'
        events.append(label('label_replace(('+q+'),"object","$1","'+obj+'","(.*)")','event',event))
    eventq='topk(5,max by(host,object,event)('+ ' or '.join(events)+') > (time()-3600))*1000'
    p=stat('RECENT EVENTS · newest five in last hour',15,8,9,4,[('{{host}} · {{object}} · {{event}}','sort_desc('+eventq+')')],unit='time:HH:mm:ss',size=16)
    p['options']['textMode']='value_and_name'
    p['fieldConfig']['defaults']['color']={'mode':'fixed','fixedColor':BLUE}
    p['description']='Sampled starts, reboots, OOM increments and state/health/probe transitions. Five newest only. This is not an exact or complete Docker event log.'
    # Region 3: three fleet rows, shared scale and fixed columns.
    p=panel('HOSTS · pressure and headroom',0,12,24,6,'table','Host state covers scrape availability and sustained resource use; domain alerts remain in the header. Container counts are observed, not desired-state inventory. Network is bytes/s, not link utilization.')
    host_metrics=[('State',HOST_STATE,'short'),('CPU',CPU,'percentunit'),('Memory',MEM,'percentunit'),('Disk',FS,'percentunit'),
        ('Receive','sum by(host)(rate('+m('node_network_receive_bytes_total','device!~"lo|veth.*|br-.*|docker.*|tailscale.*"',N)+'[1m]))','Bps'),
        ('Transmit','sum by(host)(rate('+m('node_network_transmit_bytes_total','device!~"lo|veth.*|br-.*|docker.*|tailscale.*"',N)+'[1m]))','Bps'),
        ('Temp','max by(host)('+m('node_hwmon_temp_celsius',scope=N)+')','celsius'),
        ('Uptime','time()-max by(host)('+m('node_boot_time_seconds',scope=N)+')','s'),
        ('Containers','sum by(host)('+RUN+')','short'),('Abnormal',CONTAINER_BAD,'short')]
    p['targets']=[target(q,chr(65+i),table=True) for i,(_,q,_) in enumerate(host_metrics)]
    names={'host':'Host',**{'Value #'+chr(65+i):title for i,(title,_,_) in enumerate(host_metrics)}}
    p['transformations']=[{'id':'joinByField','options':{'byField':'host','mode':'outerTabular'}},{'id':'filterFieldsByName','options':{'include':{'names':list(names)}}},{'id':'organize','options':{'indexByName':{n:i for i,n in enumerate(names)},'renameByName':names}}]
    p['options']={'showHeader':True,'cellHeight':'md','sortBy':[{'displayName':'Host','desc':False}],'footer':{'show':False}}
    p['fieldConfig']['defaults']['mappings']=[{'type':'special','options':{'match':'null','result':{'text':'UNKNOWN','color':UNKNOWN}}}]
    p['fieldConfig']['defaults']['custom']={'minWidth':60,'align':'center','cellOptions':{'type':'auto'}}
    override(p,'Host',{'custom.width':150,'custom.align':'left','links':[{'title':'Host detail','url':'/d/technis-host?var-host=${__value.text}'}]})
    override(p,'State',{'mappings':MAP,'custom.cellOptions':{'type':'color-text'}})
    for name,_,unit in host_metrics:
        props={'unit':unit,'decimals':1 if unit=='percentunit' else 0}
        if name in ('CPU','Memory','Disk','Temp','Abnormal'):
            values=(.80,.92) if unit=='percentunit' else ((75,90) if name=='Temp' else (1,3))
            props.update({'custom.cellOptions':{'type':'color-text'},'thresholds':{'mode':'absolute','steps':[{'color':GOOD,'value':None},{'color':WARN,'value':values[0]},{'color':BAD,'value':values[1]}]}})
        override(p,name,props)
    # Region 4: small trends; only pressure indicators, no wall of graphs.
    for i,(title,q,unit) in enumerate([
        ('CPU pressure · 1h',m('technis_pressure_average_ratio','resource="cpu",scope="some",window="10"',N),'percentunit'),
        ('Memory pressure · 1h',m('technis_pressure_average_ratio','resource="memory",scope="some",window="10"',N),'percentunit'),
        ('I/O pressure · 1h',m('technis_pressure_average_ratio','resource="io",scope="some",window="10"',N),'percentunit'),
        ('Slowest HTTP checks · 1h','topk(3,max by(name)('+m('gatus_results_duration_seconds')+'))','s')]):
        p=panel(title,i*6,18,6,5,'timeseries')
        p['targets']=[target(q,name='{{host}}' if i<3 else '{{name}}',history=True)]
        p['fieldConfig']['defaults'].update({'unit':unit,'decimals':2,'color':{'mode':'palette-classic'},'min':0,'custom':{'lineWidth':1,'fillOpacity':5,'spanNulls':False,'axisLabel':'','axisBorderShow':False,'showPoints':'never'}})
        p['options']={'legend':{'displayMode':'list','placement':'bottom','calcs':[]},'tooltip':{'mode':'multi','sort':'desc'}}
    # Region 5: coverage limitations stay visible rather than being mistaken for health.
    p=stat('COVERAGE / RECENT CHANGES',0,23,16,3,[('No health check','sum(('+RUN+' == 1) unless on(host,container) ('+m('technis_docker_container_health',EXCLUDE)+' == 1)) or vector(0)'),('Start changes / 15m','sum(changes('+m('container_start_time_seconds','name!=""')+'[15m]))'),('OOM / 15m','sum(increase('+m('container_oom_events_total','name!=""')+'[15m]))')],size=22)
    p['description']='No-health-check count excludes Docker health states but does not imply no external HTTP coverage. Start changes detect restarts of an existing cAdvisor series; recreated containers can appear as a new series and are not an exact restart counter. OOM is sampled counter growth.'
    p=panel('READING THIS CONSOLE',16,23,8,3,'text')
    p.pop('datasource');p['options']={'mode':'markdown','content':'**OK** observed pass · **UNKNOWN** missing evidence · **WARN / FAIL** action\n\n15 s refresh · 1 h trends · [Hosts](/d/technis-host) · [Containers](/d/technis-cgroups) · [Storage](/d/technis-disks)'}
    OUT.write_text(json.dumps(D,indent=2)+'\n')
    return D


if __name__=='__main__':
    d=generate()
    print(f'Generated {OUT}: {len(d["panels"])} native panels, 26 grid rows')
