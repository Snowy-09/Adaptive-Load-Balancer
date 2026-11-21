from flask import Flask, jsonify, render_template, request
from pathlib import Path
import json, os, tempfile, time, subprocess, signal, uuid

app = Flask(__name__, template_folder="dashboard/templates", static_folder="dashboard/static")
PROJECT_DIR = Path(__file__).resolve().parent
HEALTH_FILE = PROJECT_DIR / "runtime" / "cn_sdn_backend_health.json"
CONFIG_FILE = PROJECT_DIR / "dashboard" / "config.json"
JOB_FILE = PROJECT_DIR / "runtime" / "cn_sdn_benchmark_request.json"
RESULT_FILES = {name: PROJECT_DIR / "runtime" / f"cn_sdn_benchmark_result_{name}.json" for name in ("h1", "h2", "h3")}
CONTROLLER_LOG = Path("/tmp/cn_sdn_controller.log")
VENV_MANAGER = Path.home() / "osken-venv" / "bin" / "osken-manager"
DEFAULT_DELAYS = {"S1": 100, "S2": 300, "S3": 500}
VALID_BACKENDS = set(DEFAULT_DELAYS)
CLIENTS = ("h1", "h2", "h3")
controller_mode = "unknown"
controller_process = None
benchmark_run_id = None

def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f: return json.load(f)
    except (OSError, json.JSONDecodeError): return default

def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path): os.unlink(temp_path)

def get_delays():
    saved = read_json(CONFIG_FILE, {}).get("delays_ms", {})
    out = {}
    for name, default in DEFAULT_DELAYS.items():
        try: value = int(saved.get(name, default))
        except (TypeError, ValueError): value = default
        out[name] = max(0, min(5000, value))
    return out

def get_metrics():
    data = read_json(HEALTH_FILE, {})
    statuses, metrics = data.get("servers", {}), data.get("metrics", {})
    return {"timestamp": data.get("timestamp"), "servers": {n: {"status": statuses.get(n, "UNKNOWN"), "metrics": metrics.get(n, {})} for n in sorted(VALID_BACKENDS)}, "delays_ms": get_delays(), "metrics_available": bool(data)}

def find_controller_pids():
    found=[]
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit(): continue
        try:
            cmd=(entry/"cmdline").read_bytes().replace(b"\0",b" ").decode(errors="ignore")
            if "osken-manager" in cmd and ("load_balancer.py" in cmd or "adaptive_load_balancer.py" in cmd): found.append(int(entry.name))
        except (OSError, ProcessLookupError): pass
    return found

@app.get("/")
def index(): return render_template("index.html")
@app.get("/api/health")
def health(): return jsonify({"status":"ok","time":time.time()})
@app.get("/api/metrics")
def api_metrics(): return jsonify(get_metrics())
@app.get("/api/delays")
def api_get_delays(): return jsonify({"delays_ms":get_delays()})
@app.put("/api/delays")
def api_set_delays():
    body=request.get_json(silent=True)
    if not isinstance(body,dict): return jsonify({"error":"Expected a JSON object"}),400
    supplied=body.get("delays_ms",body)
    if not isinstance(supplied,dict): return jsonify({"error":"delays_ms must be an object"}),400
    updated=get_delays()
    for name,value in supplied.items():
        if name not in VALID_BACKENDS: return jsonify({"error":f"Unknown backend: {name}"}),400
        try: delay=int(value)
        except (TypeError,ValueError): return jsonify({"error":f"{name} delay must be an integer"}),400
        if not 0<=delay<=5000: return jsonify({"error":f"{name} delay must be between 0 and 5000 ms"}),400
        updated[name]=delay
    write_json_atomic(CONFIG_FILE,{"delays_ms":updated,"updated_at":time.time()})
    return jsonify({"ok":True,"delays_ms":updated})

@app.get("/api/controller")
def get_controller(): return jsonify({"mode":controller_mode})
@app.post("/api/controller")
def set_controller():
    global controller_mode,controller_process
    mode=(request.get_json(silent=True) or {}).get("mode")
    if mode not in ("static","adaptive"): return jsonify({"error":"Mode must be static or adaptive"}),400
    script="load_balancer.py" if mode=="static" else "adaptive_load_balancer.py"
    if not VENV_MANAGER.exists(): return jsonify({"error":f"OS-Ken executable not found: {VENV_MANAGER}"}),500
    pids=find_controller_pids()
    for pid in pids:
        try: os.kill(pid,signal.SIGTERM)
        except ProcessLookupError: pass
    deadline=time.time()+4
    while time.time()<deadline and any(Path(f"/proc/{p}").exists() for p in pids): time.sleep(.1)
    for pid in pids:
        if Path(f"/proc/{pid}").exists():
            try: os.kill(pid,signal.SIGKILL)
            except ProcessLookupError: pass
    try:
        log=open(CONTROLLER_LOG,"a",buffering=1)
        controller_process=subprocess.Popen([str(VENV_MANAGER),"--verbose",script],cwd=str(PROJECT_DIR),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        time.sleep(1)
        if controller_process.poll() is not None:
            controller_mode="unknown"; return jsonify({"error":"Controller exited during startup. Check /tmp/cn_sdn_controller.log"}),500
        controller_mode=mode
        return jsonify({"ok":True,"mode":mode,"pid":controller_process.pid})
    except OSError as exc:
        controller_mode="unknown"; return jsonify({"error":str(exc)}),500

@app.post("/api/benchmark")
def start_benchmark():
    global benchmark_run_id
    body=request.get_json(silent=True) or {}
    try: total=int(body.get("total",200)); workers=int(body.get("workers",10))
    except (TypeError,ValueError): return jsonify({"error":"Requests and workers must be integers"}),400
    if not 10<=total<=500: return jsonify({"error":"Choose between 10 and 500 requests per client"}),400
    if not 1<=workers<=50: return jsonify({"error":"Workers per client must be between 1 and 50"}),400
    missing_agents=[]
    for client in CLIENTS:
        heartbeat=PROJECT_DIR / "runtime" / f"cn_sdn_agent_{client}_heartbeat"
        try:
            age=time.time()-float(heartbeat.read_text(encoding="utf-8").strip())
            if age>15: missing_agents.append(client)
        except (OSError, ValueError):
            missing_agents.append(client)
    if missing_agents:
        return jsonify({"error":"Start the benchmark agents inside Mininet for: " + ", ".join(missing_agents)}),503
    benchmark_run_id=uuid.uuid4().hex
    for path in RESULT_FILES.values():
        try: path.unlink()
        except FileNotFoundError: pass
    write_json_atomic(JOB_FILE,{"run_id":benchmark_run_id,"total":total,"workers":workers,"clients":list(CLIENTS),"created_at":time.time()})
    return jsonify({"ok":True,"run_id":benchmark_run_id,"clients":list(CLIENTS),"total_per_client":total,"total_requests":total*len(CLIENTS)})

def aggregate_results(run_id):
    per_client={name:read_json(path,{}) for name,path in RESULT_FILES.items()}
    if not all(data.get("run_id")==run_id for data in per_client.values()): return None
    total=sum(int(x.get("total",0)) for x in per_client.values())
    successful=sum(int(x.get("successful",0)) for x in per_client.values())
    failed=total-successful
    start_times=[float(x.get("started_at", 0)) for x in per_client.values()]
    finish_times=[float(x.get("finished_at", 0)) for x in per_client.values()]
    elapsed=(max(finish_times)-min(start_times)) if start_times and min(start_times)>0 else max(float(x.get("elapsed_seconds",0)) for x in per_client.values())
    latencies=sorted(v for x in per_client.values() for v in x.get("latencies",[]))
    p95=latencies[max(0,int(.95*len(latencies))-1)] if latencies else 0
    distribution={}
    for x in per_client.values():
        for name,count in x.get("distribution",{}).items(): distribution[name]=distribution.get(name,0)+int(count)
    return {"run_id":run_id,"clients":len(CLIENTS),"per_client":{n:{"total":x["total"],"successful":x["successful"],"failed":x["failed"],"elapsed_seconds":x["elapsed_seconds"],"throughput":x["throughput"],"distribution":x["distribution"]} for n,x in per_client.items()},"total":total,"successful":successful,"failed":failed,"elapsed_seconds":round(elapsed,4),"throughput":round(successful/elapsed,2) if elapsed else 0,"average_latency":round(sum(latencies)/len(latencies),2) if latencies else 0,"p95_latency":round(p95,2),"distribution":distribution,"errors":[e for x in per_client.values() for e in x.get("errors",[])][:10],"finished_at":time.time()}

@app.get("/api/benchmark/status")
def benchmark_status():
    job=read_json(JOB_FILE,{})
    run_id=benchmark_run_id or job.get("run_id")
    if run_id:
        result=aggregate_results(run_id)
        if result: return jsonify({"state":"complete","result":result})
        if job.get("run_id")==run_id: return jsonify({"state":"running","run_id":run_id,"clients_ready":sum(read_json(p,{}).get("run_id")==run_id for p in RESULT_FILES.values()),"clients_total":len(CLIENTS)})
    return jsonify({"state":"idle"})

if __name__=="__main__": app.run(host="0.0.0.0",port=5000,debug=False)
