"""Fixed script sent over authenticated SSH stdin (not installed on the VPS).

Reads the single startup JSON and invokes only the local read-only lso command
after verifying that the listener belongs to that exact Xray process.
"""
SNAPSHOT_SCRIPT = r'''import os, re, pathlib
def discover():
 hits=[]
 for p in pathlib.Path("/proc").iterdir():
  if not p.name.isdecimal(): continue
  try:
   exe=os.readlink(p/"exe")
   if not re.fullmatch(r"xray(?:[-_][\w-]+)?",pathlib.Path(exe).name): continue
   argv=(p/"cmdline").read_bytes().decode().strip("\0").split("\0")
   cfg=[]; dirs=[]
   for j,a in enumerate(argv[1:],1):
    k,sep,v=a.partition("=")
    if k in ("-c","-config","--config","--confdir","-confdir"):
     val=v if sep else argv[j+1]
     (dirs if "confdir" in k else cfg).append(val)
   cwd=(p/"cwd").resolve(strict=True)
   hits.append((p,exe,[str((cwd/v).resolve(strict=True)) for v in cfg],dirs))
  except (OSError,ValueError,IndexError): continue
 return hits
import json, stat, subprocess, ipaddress
def safe_get(x):
 return x if isinstance(x,dict) else {}
def main():
 hits=discover()
 assert len(hits)==1
 p,exe,files,dirs=hits[0]
 assert len(files)==1 and not dirs
 st=os.stat(files[0])
 assert stat.S_ISREG(st.st_mode) and st.st_size<8*1024*1024
 with open(files[0],"rb") as f: d=json.load(f)
 def stable():
  s=os.stat(files[0])
  return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)==(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns) and discover()==hits
 api=d.get("api") or {}
 api_tag=api.get("tag")
 dest=api.get("listen")
 def address_port(dest):
  h,sep,port=dest.rpartition(":")
  h=h.strip("[]")
  assert sep and ipaddress.ip_address(h).is_loopback and port.isascii() and port.isdecimal() and 1<=int(port)<=65535
  return h,int(port)
 if dest:
  h,pt=address_port(dest)
 else:
  ins=[i for i in d.get("inbounds",[]) if i.get("tag")==api_tag]
  assert len(ins)==1
  i=ins[0]
  h=i.get("listen","")
  pt=i.get("port")
  assert ipaddress.ip_address(h).is_loopback and type(pt) is int and 1<=pt<=65535
 assert "HandlerService" in api.get("services",[])
 inodes=set()
 for fd in (p/"fd").iterdir():
  try:
   m=re.fullmatch(r"socket:\[(\d+)\]",os.readlink(fd))
   if m: inodes.add(m.group(1))
  except OSError: pass
 owner=False
 for line in (p/"net"/("tcp6" if ":" in h else "tcp")).read_text().splitlines()[1:]:
  cells=line.split()
  addr,port=cells[1].split(":")
  raw=bytes.fromhex(addr); raw=b"".join(raw[j:j+4][::-1] for j in range(0,len(raw),4))
  if int(port,16)==pt and ipaddress.ip_address(raw)==ipaddress.ip_address(h) and cells[3]=="0A" and cells[9] in inodes: owner=True
 assert owner
 ap=f"[{h}]:{pt}" if ":" in h else f"{h}:{pt}"
 rr=subprocess.run([exe,"api","lso","--server="+ap,"--timeout=4"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=6)
 if rr.returncode!=0 or len(rr.stdout)>1024*1024:
  print(json.dumps({"stage":"runtime_lso_failed","exit":rr.returncode})); return
 runtime=json.loads(rr.stdout)
 assert stable()
 disk=d.get("outbounds") or []
 obs=runtime.get("outbounds") or []
 def shaped(x): return sorted(k for k in x if re.fullmatch("[a-zA-Z_]+",k))
 def only_shape(x,depth):
  if not isinstance(x,dict) or depth<=0: return type(x).__name__
  return {k:only_shape(x[k],depth-1) for k in shaped(x) if k not in ("users","user","account","password","accounts","email")}
 def typed(o):
  return safe_get(o.get("proxySettings")).get("_TypedMessage_","")
 def protocol(o):
  t=typed(o)
  for n in ("socks","http","freedom","blackhole"):
   if isinstance(t,str) and t.startswith("xray.proxy."+n+"."): return n
  return "other"
 def runtime(o):
  settings=safe_get(o.get("proxySettings"))
  srv=safe_get(settings.get("server"))
  sender=safe_get(o.get("senderSettings"))
  stream=safe_get(sender.get("streamSettings"))
  sock=safe_get(stream.get("socketSettings") or stream.get("sockopt"))
  chain=bool(sender.get("proxySettings") or sock.get("dialerProxy"))
  addrs=[[srv.get("address"),srv.get("port")]] if protocol(o) in ("socks","http") else []
  return [o.get("tag"),protocol(o),addrs,chain]
 def file(o):
  srv=safe_get(o.get("settings")).get("servers") or []
  addrs=[[safe_get(a).get("address"),safe_get(a).get("port")] for a in srv]
  sock=safe_get(safe_get(o.get("streamSettings")).get("sockopt"))
  return [o.get("tag"),o.get("protocol"),addrs,bool(o.get("proxySettings") or sock.get("dialerProxy"))]
 def route(rule):
  known={"user","inboundTag","ip","protocol","outboundTag","balancerTag","ruleTag","type","domain","port","network"}
  return {**{k:rule[k] for k in known if k in rule}, "unsupported":bool(rule.keys()-known)}
 routing=safe_get(d.get("routing"))
 pid_start=int((p/"stat").read_text().rsplit(")",1)[1].split()[19])
 boot=next(int(l.split()[1]) for l in open("/proc/stat") if l.startswith("btime "))
 start=boot+pid_start/os.sysconf("SC_CLK_TCK")
 assert len(obs)<=512 and len(disk)<=512 and len(routing.get("rules") or [])<=2048
 print(json.dumps({"stage":"ok","process":[int(p.name),pid_start],"disk":[file(o) for o in disk],
    "runtime":sorted([runtime(o) for o in obs],key=lambda o:o[0] or ""),
    "started_from_file":st.st_mtime<=start+1,
    "script":bool(routing.get("script")),"rules":[route(r) for r in routing.get("rules",[])]}))
try: main()
except Exception: print('{"stage":"error"}')
'''
