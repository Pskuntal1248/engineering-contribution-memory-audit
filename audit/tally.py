import json,glob,collections,sys,os
d=sorted(glob.glob('.gravity/runs/*/'),key=os.path.getmtime)[-1]
ev=[json.loads(l) for l in open(d+'events.jsonl')]
c=collections.Counter(e['tool'] for e in ev if e['type']=='tool.called')
print(os.path.basename(d.rstrip('/')),'| integration tool calls:',sum(c.values()),dict(c),'| model.called:',sum(1 for e in ev if e['type']=='model.called'))
