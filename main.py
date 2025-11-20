from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from datetime import datetime, timezone
from pathlib import Path
import csv
import io
import json
import zipfile

app = FastAPI()

# ---------- Paths ----------
BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# ---------- Global state ----------
logging_enabled = False
start_epoch = None
current_session_id = None
current_session_dir: Path | None = None  # active session directory


def utc_now_iso() -> str:
    """UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_session_id() -> str:
    """Folder-friendly timestamp ID."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ---------- CSV Helpers ----------
def get_device_csv_path(device_id: str) -> Path:
    assert current_session_dir is not None
    return current_session_dir / f"{device_id}.csv"


def ensure_device_csv(device_id: str, fieldnames: list[str]) -> None:
    """
    Create CSV for this device (if not exists) with header:
    server_time_utc, device_id, <fieldnames...>
    """
    csv_path = get_device_csv_path(device_id)
    if csv_path.exists():
        return

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["server_time_utc", "device_id"] + fieldnames)


def append_samples(device_id: str, samples: list[dict], fieldnames: list[str]) -> None:
    """Append sample rows to device CSV."""
    csv_path = get_device_csv_path(device_id)
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for s in samples:
            row = [utc_now_iso(), device_id]
            for key in fieldnames:
                row.append(s.get(key, ""))
            writer.writerow(row)


# ---------- ROUTES ----------
@app.get("/")
async def root():
    return HTMLResponse("<h2>IoT Logger</h2><p>Go to <a href='/dashboard'>Dashboard</a></p>")


@app.get("/dashboard")
async def dashboard():
    html = (BASE_DIR / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ---------------- START SESSION ----------------
@app.post("/api/start")
async def api_start():
    """
    Start new logging session:
    - enables logging
    - sets start_epoch
    - creates session directory
    """
    global logging_enabled, start_epoch, current_session_id, current_session_dir

    if logging_enabled:
        return {
            "status": "already_running",
            "session_id": current_session_id,
            "start_epoch": start_epoch,
        }

    logging_enabled = True
    now = datetime.now(timezone.utc)
    start_epoch = int(now.timestamp())
    current_session_id = new_session_id()
    current_session_dir = SESSIONS_DIR / current_session_id
    current_session_dir.mkdir(exist_ok=True)

    return {
        "status": "started",
        "session_id": current_session_id,
        "start_epoch": start_epoch,
        "start_time_utc": now.isoformat().replace("+00:00", "Z"),
    }


# ---------------- STOP SESSION ----------------
@app.post("/api/stop")
async def api_stop():
    """
    Stop logging, return ZIP of all CSV files.
    """
    global logging_enabled, start_epoch, current_session_id, current_session_dir

    if current_session_dir is None or current_session_id is None:
        raise HTTPException(status_code=400, detail="No active session to stop")

    session_dir = current_session_dir
    session_id = current_session_id

    # Reset state
    logging_enabled = False
    start_epoch = None
    current_session_id = None
    current_session_dir = None

    # Build ZIP in memory
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for csv_file in session_dir.glob("*.csv"):
            zf.write(csv_file, arcname=csv_file.name)
    mem.seek(0)

    return StreamingResponse(
        mem,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="session_{session_id}.zip"'
        }
    )


# ---------------- CONFIG FOR ARDUINO ----------------
@app.get("/api/config")
async def api_config(device_id: str):
    """
    Arduino polls this.
    Returns:
    - logging: True/False (server is currently logging)
    - start_epoch: when logging began (None if not started)
    """
    return {
        "logging": logging_enabled,
        "start_epoch": start_epoch,
    }


# ---------------- BULK UPLOAD FROM ARDUINO ----------------
@app.post("/api/bulk_samples")
async def api_bulk_samples(request: Request):
    """
    Body expected:
    {
      "device_id": "tof_01",
      "samples": [
         {"timestamp_utc": "...",
          "sensor_time_ms": ...,
          "distance_m": ...,
          "status": ...,
          "signal": ...,
          "precision_cm": ...}
      ]
    }
    """
    global logging_enabled, current_session_dir

    # If not logging, ignore data silently (Arduino keeps running)
    if not logging_enabled or current_session_dir is None:
        return {"status": "ignored", "reason": "not_logging"}

    # Parse JSON
    body = await request.body()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    device_id = data.get("device_id")
    samples = data.get("samples")

    if not isinstance(device_id, str):
        raise HTTPException(status_code=400, detail="device_id must be string")

    if not isinstance(samples, list):
        raise HTTPException(status_code=400, detail="samples must be list")

    if len(samples) == 0:
        return {"status": "ok", "written": 0}

    # Determine fieldnames from first sample
    first = samples[0]
    if not isinstance(first, dict):
        raise HTTPException(status_code=400, detail="Invalid sample object")

    fieldnames = sorted(first.keys())

    # Create CSV if needed
    ensure_device_csv(device_id, fieldnames)

    # Append rows
    append_samples(device_id, samples, fieldnames)

    return {"status": "ok", "written": len(samples)}
