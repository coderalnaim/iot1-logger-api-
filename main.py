from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse
from datetime import datetime
from pathlib import Path
import json
import re

app = FastAPI(title="Universal Sensor Logging API (with required timestamp format)")

# log folder
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# device state
device_state = {}

ISO_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
)

def create_logfile(device_id: str) -> Path:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file = LOG_DIR / f"{device_id}_{ts}.jsonl"
    return file


@app.post("/api/control")
def control(req: dict = Body(...)):
    device_id = req.get("device_id")
    enabled = req.get("enabled")

    if device_id is None:
        raise HTTPException(400, "Missing device_id")

    state = device_state.get(device_id, {"enabled": False, "file": None})

    if enabled and not state["enabled"]:
        file = create_logfile(device_id)
        state["file"] = file
        state["enabled"] = True
    elif not enabled and state["enabled"]:
        state["enabled"] = False

    device_state[device_id] = state
    return {"device_id": device_id, "enabled": state["enabled"], "file_path": str(state["file"])}


@app.post("/api/measurement")
def measurement(payload: dict = Body(...)):
    """
    Requires:
      device_id
      timestamp_utc (ISO8601, ending in Z)
      sensor_time_ms (integer)
    """
    if "device_id" not in payload:
        raise HTTPException(400, "Missing device_id")
    if "timestamp_utc" not in payload:
        raise HTTPException(400, "Missing timestamp_utc")
    if not ISO_REGEX.match(payload["timestamp_utc"]):
        raise HTTPException(400, "timestamp_utc must be ISO 8601 with Z")

    if "sensor_time_ms" not in payload:
        raise HTTPException(400, "Missing sensor_time_ms")

    device_id = payload["device_id"]
    state = device_state.get(device_id, {"enabled": False, "file": None})

    if not state["enabled"] or state["file"] is None:
        return {"stored": False, "reason": "logging_disabled"}

    # Add server time (optional)
    record = {
        "server_time_utc": datetime.utcnow().isoformat() + "Z",
        **payload
    }

    with state["file"].open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return {"stored": True}


@app.get("/api/status")
def status():
    return {
        dev: {"enabled": s["enabled"], "file": str(s["file"])}
        for dev, s in device_state.items()
    }


# ---------------- dashboard ----------------
@app.get("/dashboard", response_class=HTMLResponse)
def ui():
    html = """
<html><body>
<h1>Sensor Logger Dashboard</h1>
<p>Start/Stop logging for any device ID.</p>

<input id="id" placeholder="device id"/>
<button onclick="start()">Start</button>
<button onclick="stop()">Stop</button>

<pre id="out"></pre>

<script>
async function start(){
  const id=document.getElementById('id').value;
  const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id,enabled:true})});
  out.textContent=JSON.stringify(await r.json(),null,2);
}
async function stop(){
  const id=document.getElementById('id').value;
  const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id,enabled:false})});
  out.textContent=JSON.stringify(await r.json(),null,2);
}
setInterval(async()=>{
  const r=await fetch('/api/status');
  out.textContent=JSON.stringify(await r.json(),null,2);
},2000);
</script>
</body></html>
"""
    return HTMLResponse(html)
