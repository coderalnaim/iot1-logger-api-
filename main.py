from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from datetime import datetime, timezone
from pathlib import Path
import csv
import io
import json
import zipfile
import threading

app = FastAPI()

# ---------- Paths ----------
BASE_DIR = Path(__file__).parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

SESSION_STATE_FILE = BASE_DIR / "session_state.json"

# ---------- In-memory cache + lock ----------
state_lock = threading.Lock()

# These are just local caches for this process
logging_enabled: bool = False
start_epoch: int = 0
current_session_id: str | None = None
current_session_dir: Path | None = None


# =============== STATE HELPERS (shared across workers) ===============
def default_state() -> dict:
    return {
        "logging": False,
        "start_epoch": 0,
        "session_id": None,
    }


def load_state() -> dict:
    if not SESSION_STATE_FILE.exists():
        return default_state()
    try:
        with SESSION_STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # ensure required keys
        for k, v in default_state().items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default_state()


def save_state(state: dict) -> None:
    with SESSION_STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f)


def sync_from_state(state: dict) -> None:
    """Copy shared state into this worker's globals."""
    global logging_enabled, start_epoch, current_session_id, current_session_dir
    logging_enabled = bool(state.get("logging", False))
    start_epoch = int(state.get("start_epoch", 0) or 0)
    sid = state.get("session_id")
    current_session_id = sid
    current_session_dir = SESSIONS_DIR / sid if sid else None


def new_session_id() -> str:
    """Folder-friendly timestamp ID."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def utc_now_iso() -> str:
    """UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


# =============== CSV HELPERS ===============
def get_device_csv_path(device_id: str) -> Path:
    """
    Uses the current_session_dir if available, otherwise falls back to a
    global orphan file (should only happen in weird edge cases).
    """
    global current_session_dir
    if current_session_dir is None:
        return SESSIONS_DIR / f"orphan_{device_id}.csv"
    return current_session_dir / f"{device_id}.csv"


def ensure_device_csv(device_id: str, fieldnames: list[str]) -> None:
    csv_path = get_device_csv_path(device_id)
    if csv_path.exists():
        return

    csv_path.parent.mkdir(exist_ok=True, parents=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["server_time_utc", "device_id"] + fieldnames)


def append_samples(device_id: str, samples: list[dict], fieldnames: list[str]) -> None:
    csv_path = get_device_csv_path(device_id)
    csv_path.parent.mkdir(exist_ok=True, parents=True)
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for s in samples:
            row = [utc_now_iso(), device_id]
            for key in fieldnames:
                row.append(s.get(key, ""))
            writer.writerow(row)


# =============== ROUTES ===============
@app.get("/")
async def root():
    return HTMLResponse("<h2>IoT Logger</h2><p>Go to <a href='/dashboard'>Dashboard</a></p>")


@app.get("/dashboard")
async def dashboard():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>IoT Dashboard</title>
        <style>
            body { font-family: sans-serif; padding: 20px; text-align: center; }
            button { padding: 15px 30px; font-size: 18px; margin: 10px; cursor: pointer; }
            .status { margin-top: 20px; padding: 10px; border: 1px solid #ccc; display: inline-block; }
            .running { background-color: #d4edda; color: #155724; }
            .stopped { background-color: #f8d7da; color: #721c24; }
        </style>
    </head>
    <body>
        <h1>IoT Logger Control</h1>
        <div>
            <button onclick="startSession()">START Logging</button>
            <button onclick="stopSession()">STOP & Download</button>
        </div>
        <div id="statusBox" class="status stopped">Status: Stopped</div>
        <pre id="info"></pre>

        <script>
            async function updateStatus() {
                try {
                    let res = await fetch('/api/config?device_id=browser');
                    let data = await res.json();
                    let box = document.getElementById('statusBox');
                    if (data.logging) {
                        box.className = "status running";
                        box.innerText = "Status: LOGGING (start_epoch: " + data.start_epoch + ")";
                    } else {
                        box.className = "status stopped";
                        box.innerText = "Status: STOPPED";
                    }
                } catch(e) { console.error(e); }
            }

            async function startSession() {
                let res = await fetch('/api/start', {method: 'POST'});
                let data = await res.json();
                document.getElementById('info').innerText = JSON.stringify(data, null, 2);
                updateStatus();
            }

            async function stopSession() {
                window.location.href = '/api/stop';
                setTimeout(updateStatus, 1000);
            }

            setInterval(updateStatus, 2000);
            updateStatus();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


# ---------------- START SESSION ----------------
@app.post("/api/start")
async def api_start():
    global current_session_dir

    with state_lock:
        state = load_state()
        if state["logging"] and state.get("session_id"):
            # Already running – sync globals and return existing
            sync_from_state(state)
            return {
                "status": "already_running",
                "session_id": state["session_id"],
                "start_epoch": state["start_epoch"],
            }

        now = datetime.now(timezone.utc)
        start_epoch = int(now.timestamp())
        session_id = new_session_id()
        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(exist_ok=True, parents=True)

        # Update state on disk
        state["logging"] = True
        state["start_epoch"] = start_epoch
        state["session_id"] = session_id
        save_state(state)

        # Sync into this worker
        sync_from_state(state)
        current_session_dir = session_dir

        print(f"✅ Session STARTED: {session_id}")

        return {
            "status": "started",
            "session_id": session_id,
            "start_epoch": start_epoch,
            "start_time_utc": now.isoformat().replace("+00:00", "Z"),
        }


# ---------------- STOP SESSION ----------------
@app.get("/api/stop")
async def api_stop():
    with state_lock:
        state = load_state()
        session_id = state.get("session_id")
        if not session_id:
            return {"status": "error", "message": "No active session"}

        session_dir = SESSIONS_DIR / session_id

        # Reset shared state
        state["logging"] = False
        state["start_epoch"] = 0
        # Keep session_id in state for reference, or set to None
        state["session_id"] = None
        save_state(state)

        # Update local globals
        sync_from_state(state)

        print(f"🛑 Session STOPPED: {session_id}")

    # Build ZIP in memory
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if session_dir.exists():
            files = list(session_dir.glob("*.csv"))
        else:
            files = []

        if not files:
            zf.writestr("empty.txt", "No data collected.")
        else:
            for csv_file in files:
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
    # Always read from shared state so all workers agree
    state = load_state()
    return {
        "logging": state["logging"],
        "start_epoch": state["start_epoch"],
    }


# ---------------- BULK UPLOAD FROM ARDUINO ----------------
@app.post("/api/bulk_samples")
async def api_bulk_samples(request: Request):
    global current_session_dir

    body = await request.body()
    print("📥 /api/bulk_samples called")
    print(f"  body_len={len(body)}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    device_id = data.get("device_id")
    samples = data.get("samples")

    if not device_id or not isinstance(samples, list):
        print("❌ Invalid payload: missing device_id or samples")
        return {"status": "error", "reason": "invalid_format"}

    if len(samples) == 0:
        print(f"ℹ️ No samples in request for device {device_id}")
        return {"status": "ok", "written": 0}

    # Make sure this worker knows the current session from shared state
    state = load_state()
    if state.get("session_id"):
        current_session_dir = SESSIONS_DIR / state["session_id"]
    else:
        # If no active session, still save to an orphan file so nothing is lost
        current_session_dir = None

    first = samples[0]
    fieldnames = sorted(first.keys())

    print(f"  device_id={device_id}, sample_count={len(samples)}, fieldnames={fieldnames}")
    print(f"  session_dir={current_session_dir}")

    ensure_device_csv(device_id, fieldnames)
    append_samples(device_id, samples, fieldnames)

    print(f"📝 Saved {len(samples)} samples from {device_id} into {get_device_csv_path(device_id)}")

    return {"status": "ok", "written": len(samples)}
