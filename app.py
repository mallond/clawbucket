from flask import Flask, jsonify, request
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from threading import Thread
import time
import random

import docker
from docker.errors import DockerException, NotFound
from pymemcache.client.base import Client as MemcacheClient

app = Flask(__name__)

STARTED_AT = datetime.now(timezone.utc).isoformat()
HOSTNAME = socket.gethostname()
SERVICE_NAME = os.environ.get("SWARM_SERVICE", "clawbucket_clawbucket")
MEMCACHED_HOST = os.environ.get("MEMCACHED_HOST", "memcached")
MEMCACHED_PORT = int(os.environ.get("MEMCACHED_PORT", "11211"))
CHAT_KEY = "clawbucket:chat:messages"
CHAT_LIMIT = 40
ARM_EVENTS_KEY = "clawbucket:arm:events"
ARM_EVENTS_LIMIT = 500
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TTL_SECONDS = 20
RPS_STATE_KEY = "clawbucket:rps:state"
RPS_INTERVAL_KEY = "clawbucket:rps:interval_seconds"
RPS_DEFAULT_INTERVAL_SECONDS = 10
RPS_TTL_SECONDS = 20
PLAYER_SCORE_PREFIX = "clawbucket:rps:score:"
PLAYER_LAST_SEEN_PREFIX = "clawbucket:rps:last_seen:"


def color_from_text(text: str) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return f"#{digest[:6]}"


def whoami_payload():
    return {
        "hostname": HOSTNAME,
        "started_at_utc": STARTED_AT,
        "service": os.environ.get("SERVICE_NAME", "clawbucket"),
        "swarm_node": os.environ.get("SWARM_NODE", "unknown"),
        "task_name": os.environ.get("TASK_NAME", "unknown"),
        "task_slot": os.environ.get("TASK_SLOT", "unknown"),
        "task_id": os.environ.get("TASK_ID", "unknown"),
        "color": color_from_text(HOSTNAME),
    }


def docker_client():
    return docker.from_env()


def memcache_client():
    return MemcacheClient((MEMCACHED_HOST, MEMCACHED_PORT), connect_timeout=0.5, timeout=0.5)


def load_chat_messages():
    client = None
    try:
        client = memcache_client()
        raw = client.get(CHAT_KEY)
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data[-CHAT_LIMIT:]
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return []


def append_chat_message(text: str):
    text = (text or "").strip()
    if not text:
        return None

    message = {
        "id": hashlib.md5(f"{datetime.now(timezone.utc).isoformat()}:{HOSTNAME}:{text}".encode("utf-8")).hexdigest()[:12],
        "from": os.environ.get("TASK_NAME", HOSTNAME),
        "host": HOSTNAME,
        "text": text[:280],
        "at": datetime.now(timezone.utc).isoformat(),
    }

    messages = load_chat_messages()
    messages.append(message)
    messages = messages[-CHAT_LIMIT:]

    client = None
    try:
        client = memcache_client()
        client.set(CHAT_KEY, json.dumps(messages), expire=86400)
    except Exception:
        return None
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass

    return message


def load_arm_events():
    client = None
    try:
        client = memcache_client()
        raw = client.get(ARM_EVENTS_KEY)
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data[-ARM_EVENTS_LIMIT:]
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return []


def append_arm_event(task_id: str, bot_name: str, state: str):
    task_id = (task_id or "").strip()
    bot_name = (bot_name or "").strip()
    state = (state or "").strip().lower()
    if not task_id or state not in {"on", "off"}:
        return None

    event = {
        "id": hashlib.md5(f"{datetime.now(timezone.utc).isoformat()}:{task_id}:{state}".encode("utf-8")).hexdigest()[:12],
        "task_id": task_id,
        "bot": bot_name or generated_name(task_id),
        "state": state,
        "at": datetime.now(timezone.utc).isoformat(),
        "source": os.environ.get("TASK_NAME", HOSTNAME),
    }

    events = load_arm_events()
    events.append(event)
    events = events[-ARM_EVENTS_LIMIT:]

    client = None
    try:
        client = memcache_client()
        client.set(ARM_EVENTS_KEY, json.dumps(events), expire=86400)
    except Exception:
        return None
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass

    return event


def task_heartbeat_key() -> str:
    task_name = os.environ.get("TASK_NAME", HOSTNAME)
    return f"clawbucket:heartbeat:{task_name}"


def heartbeat_payload() -> str:
    task_name = os.environ.get("TASK_NAME", HOSTNAME)
    ts = datetime.now(timezone.utc).isoformat()
    return f"Ping from {task_name} at {ts}"


def write_task_heartbeat_once():
    client = None
    try:
        client = memcache_client()
        client.set(task_heartbeat_key(), heartbeat_payload(), expire=HEARTBEAT_TTL_SECONDS)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def heartbeat_loop():
    # Light simulation only: each task self-reports liveness via expiring key.
    while True:
        write_task_heartbeat_once()
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def get_rps_interval_seconds() -> int:
    client = None
    try:
        client = memcache_client()
        raw = client.get(RPS_INTERVAL_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw is None:
            return RPS_DEFAULT_INTERVAL_SECONDS
        value = int(raw)
        return max(2, min(120, value))
    except Exception:
        return RPS_DEFAULT_INTERVAL_SECONDS
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def set_rps_interval_seconds(value: int) -> int:
    value = max(2, min(120, int(value)))
    client = None
    try:
        client = memcache_client()
        client.set(RPS_INTERVAL_KEY, str(value), expire=86400)
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return value


def is_this_task_on_leader_manager() -> bool:
    # Match UI manager selection: exactly one publisher task.
    current_task_id = os.environ.get("TASK_ID")
    if not current_task_id:
        return False

    try:
        client = docker_client()
        leader_node_id = None
        for node in client.nodes.list():
            nattrs = node.attrs or {}
            mstatus = nattrs.get("ManagerStatus") or {}
            if mstatus.get("Leader") is True:
                leader_node_id = nattrs.get("ID")
                break

        service = client.services.get(SERVICE_NAME)
        tasks = []
        for task in service.tasks():
            status = task.get("Status", {})
            state = status.get("State", "unknown")
            if state in {"running", "starting", "ready", "preparing"}:
                tasks.append(task)

        tasks.sort(key=lambda t: t.get("Slot", 10**9))

        selected_task_id = None
        if leader_node_id:
            for t in tasks:
                if t.get("NodeID") == leader_node_id:
                    selected_task_id = t.get("ID")
                    break
        if selected_task_id is None and tasks:
            selected_task_id = tasks[0].get("ID")

        return bool(selected_task_id and selected_task_id == current_task_id)
    except Exception:
        return False


def task_id_for_keys() -> str:
    return os.environ.get("TASK_ID", "unknown")


def score_key(task_id: str) -> str:
    return f"{PLAYER_SCORE_PREFIX}{task_id}"


def last_seen_key(task_id: str) -> str:
    return f"{PLAYER_LAST_SEEN_PREFIX}{task_id}"


def get_task_score(task_id: str) -> int:
    client = None
    try:
        client = memcache_client()
        raw = client.get(score_key(task_id))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return int(raw) if raw is not None else 0
    except Exception:
        return 0
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def set_task_score(task_id: str, score: int):
    client = None
    try:
        client = memcache_client()
        client.set(score_key(task_id), str(int(score)), expire=86400)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def score_delta(player: str, leader: str) -> int:
    p = (player or "").lower()
    l = (leader or "").lower()
    alias = {"stone": "rock"}
    p = alias.get(p, p)
    l = alias.get(l, l)
    if p == l:
        return 0
    wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
    return 1 if (p, l) in wins else -1


def player_round_once():
    # Only non-leaders are players.
    if is_this_task_on_leader_manager():
        return

    state = read_rps_state()
    if not state:
        return

    leader_choice = (state.get("choice") or "").lower()
    round_id = state.get("at") or ""
    if not leader_choice or not round_id:
        return

    task_id = task_id_for_keys()

    client = None
    try:
        client = memcache_client()
        prev_seen = client.get(last_seen_key(task_id))
        if isinstance(prev_seen, bytes):
            prev_seen = prev_seen.decode("utf-8")
        if prev_seen == round_id:
            return

        player_choice = random.choice(["rock", "paper", "scissors"])
        delta = score_delta(player_choice, leader_choice)
        current_score = get_task_score(task_id)
        new_score = current_score + delta

        client.set(score_key(task_id), str(new_score), expire=86400)
        client.set(last_seen_key(task_id), round_id, expire=86400)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def write_rps_state_once():
    # Light simulation: only the task on Swarm leader-manager node publishes.
    if not is_this_task_on_leader_manager():
        return

    choice = random.choice(["rock", "paper", "scissors"])
    task_name = os.environ.get("TASK_NAME", HOSTNAME)
    payload = {
        "choice": choice,
        "from": task_name,
        "at": datetime.now(timezone.utc).isoformat(),
        "publisher": "leader-manager",
    }

    client = None
    try:
        client = memcache_client()
        client.set(RPS_STATE_KEY, json.dumps(payload), expire=RPS_TTL_SECONDS)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def read_rps_state():
    client = None
    try:
        client = memcache_client()
        raw = client.get(RPS_STATE_KEY)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def rps_loop():
    while True:
        write_rps_state_once()
        time.sleep(get_rps_interval_seconds())


def player_loop():
    while True:
        player_round_once()
        time.sleep(get_rps_interval_seconds())


def generated_name(task_id: str) -> str:
    adjectives = [
        "Brave",
        "Neon",
        "Swift",
        "Cosmic",
        "Lucky",
        "Mellow",
        "Nova",
        "Quantum",
        "Sunny",
        "Velvet",
    ]
    nouns = [
        "Otter",
        "Falcon",
        "Panda",
        "Lynx",
        "Tiger",
        "Comet",
        "Golem",
        "Sparrow",
        "Fox",
        "Dragon",
    ]
    h = int(hashlib.md5(task_id.encode("utf-8")).hexdigest(), 16)
    return f"{adjectives[h % len(adjectives)]} {nouns[(h // len(adjectives)) % len(nouns)]}"


def get_service_state():
    client = docker_client()
    service = client.services.get(SERVICE_NAME)
    attrs = service.attrs
    desired = attrs["Spec"]["Mode"]["Replicated"]["Replicas"]

    leader_node_id = None
    try:
        for node in client.nodes.list():
            nattrs = node.attrs or {}
            mstatus = nattrs.get("ManagerStatus") or {}
            if mstatus.get("Leader") is True:
                leader_node_id = nattrs.get("ID")
                break
    except Exception:
        pass

    tasks_raw = service.tasks()
    running = []
    for task in tasks_raw:
        status = task.get("Status", {})
        state = status.get("State", "unknown")
        if state in {"running", "starting", "ready", "preparing"}:
            task_id = task.get("ID", "unknown")
            slot = task.get("Slot", "?")
            node_id = task.get("NodeID", "unknown")
            running.append(
                {
                    "id": task_id,
                    "slot": slot,
                    "state": state,
                    "node_id": node_id,
                    "name": generated_name(task_id),
                    "color": color_from_text(task_id),
                    "is_manager": False,
                    "score": get_task_score(task_id),
                }
            )

    running.sort(key=lambda x: x["slot"])

    # Mark exactly one manager tile, chosen by Swarm leader node and lowest slot.
    if running:
        manager_idx = None
        if leader_node_id:
            for i, r in enumerate(running):
                if r["node_id"] == leader_node_id:
                    manager_idx = i
                    break
        if manager_idx is None:
            manager_idx = 0
        running[manager_idx]["is_manager"] = True

    return {
        "service": SERVICE_NAME,
        "desired_replicas": desired,
        "running_count": len(running),
        "replicas": running,
    }


@app.get("/api/whoami")
def whoami():
    return jsonify(whoami_payload())


@app.get("/api/swarm")
def api_swarm():
    try:
        return jsonify(get_service_state())
    except NotFound:
        return jsonify({"error": f"Service '{SERVICE_NAME}' not found"}), 404
    except DockerException as e:
        return jsonify({"error": f"Docker API unavailable: {str(e)}"}), 503


@app.post("/api/scale")
def api_scale():
    data = request.get_json(silent=True) or {}
    replicas = data.get("replicas")
    if not isinstance(replicas, int) or replicas < 1 or replicas > 20:
        return jsonify({"error": "replicas must be an integer between 1 and 20"}), 400

    def do_scale(target: int):
        try:
            client = docker_client()
            service = client.services.get(SERVICE_NAME)
            service.scale(target)
        except Exception:
            pass

    Thread(target=do_scale, args=(replicas,), daemon=True).start()
    return jsonify({"ok": True, "service": SERVICE_NAME, "desired_replicas": replicas, "status": "scaling"}), 202


@app.get("/api/chat")
def api_chat_get():
    return jsonify({"messages": load_chat_messages(), "source": "memcached"})


@app.post("/api/chat")
def api_chat_post():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    msg = append_chat_message(text)
    if not msg:
        return jsonify({"error": "memcached unavailable"}), 503
    return jsonify({"ok": True, "message": msg}), 201


@app.get("/api/arm/events")
def api_arm_events_get():
    return jsonify({"events": load_arm_events(), "source": "memcached"})


@app.post("/api/arm")
def api_arm_post():
    data = request.get_json(silent=True) or {}
    task_id = data.get("task_id")
    bot_name = data.get("bot")
    state = data.get("state")

    event = append_arm_event(task_id, bot_name, state)
    if not event:
        return jsonify({"error": "invalid payload or memcached unavailable"}), 400
    return jsonify({"ok": True, "event": event}), 201


@app.get("/api/rps")
def api_rps_get():
    return jsonify({
        "state": read_rps_state(),
        "interval_seconds": get_rps_interval_seconds(),
        "ttl_seconds": RPS_TTL_SECONDS,
    })


@app.post("/api/rps/config")
def api_rps_config_post():
    data = request.get_json(silent=True) or {}
    interval = data.get("interval_seconds")
    if not isinstance(interval, int):
        return jsonify({"error": "interval_seconds must be an integer"}), 400
    value = set_rps_interval_seconds(interval)
    return jsonify({"ok": True, "interval_seconds": value})


@app.get("/")
def index():
    return """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Bob's World</title>
  <style>
    :root {
      --bg: #0b1020;
      --panel: #121a30;
      --line: #2b3a67;
      --text: #e8eeff;
      --muted: #9db0e3;
      --accent: #7aa2ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      color: var(--text);
      background: radial-gradient(circle at top right, #1a2450 0%, var(--bg) 45%);
      min-height: 100vh;
    }
    .wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 40px; }
    h1 { margin: 0 0 8px; font-size: 2rem; }
    .sub { color: var(--muted); margin-bottom: 18px; }
    .panel {
      background: color-mix(in srgb, var(--panel) 90%, black);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 16px;
    }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    input[type=range] { width: 220px; }
    button {
      border: 1px solid var(--accent);
      background: #1a2a57;
      color: white;
      border-radius: 10px;
      padding: 8px 14px;
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled { opacity: .6; cursor: default; }
    .meta { color: var(--muted); font-size: .95rem; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
      gap: 12px;
    }
    .chip {
      border: 1px solid var(--line);
      background: #0f1730;
      border-radius: 14px;
      padding: 12px;
      position: relative;
      overflow: hidden;
    }
    .chip::before {
      content: "";
      position: absolute;
      left: 0; top: 0; right: 0;
      height: 5px;
      background: var(--chip-color, #5b7cff);
    }
    .chip h3 { margin: 4px 0 8px; font-size: 1.02rem; }
    .chip p { margin: 4px 0; color: var(--muted); font-size: .9rem; }
    .status { display: inline-block; font-size: .78rem; font-weight: 700; letter-spacing: .02em; padding: 4px 8px; border-radius: 999px; background: #24314f; }
    .status.on { background: #2d7c4a; }
    .arm-btn {
      margin-top: 8px;
      border: 1px solid #4d5f8e;
      background: #1d2746;
      color: #fff;
      border-radius: 8px;
      padding: 6px 10px;
      font-weight: 700;
      cursor: pointer;
    }
    .arm-btn.on {
      background: #2d7c4a;
      border-color: #3fb96d;
    }
    .chip.manager {
      border-color: #d6b45b;
      box-shadow: inset 0 0 0 1px rgba(214, 180, 91, 0.35);
    }
    .manager-badge {
      display: inline-block;
      margin-left: 6px;
      font-size: .7rem;
      font-weight: 800;
      color: #201700;
      background: #d6b45b;
      border-radius: 999px;
      padding: 3px 7px;
    }
    .score {
      margin-top: 8px;
      font-size: 1.8rem;
      font-weight: 800;
      line-height: 1;
    }
    .score.positive { color: #44d27a; }
    .score.negative { color: #ff6b6b; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>🌍 Bob's World</h1>
    <div class=\"sub\">Swarm replicas as chicklets + quick scaling control.</div>

    <div class=\"panel\">
      <div class=\"row\">
        <strong id=\"svc\">service: ...</strong>
        <span class=\"meta\" id=\"counts\">running: - / desired: -</span>
      </div>
      <div class=\"row\" style=\"margin-top:12px\">
        <label for=\"replicas\">Replicas:</label>
        <input id=\"replicas\" type=\"number\" min=\"1\" max=\"25\" step=\"1\" value=\"3\" style=\"width:90px\" />
        <button id=\"apply\">Scale Swarm</button>
        <span class=\"meta\" id=\"msg\"></span>
      </div>
    </div>

    <div class=\"grid\" id=\"grid\"></div>

    <div class=\"panel\" style=\"margin-top:16px\">
      <div class=\"row\">
        <strong>Rock / Paper / Scissors Broadcast (Memcached)</strong>
      </div>
      <div class=\"row\" style=\"margin-top:10px\">
        <label for=\"rpsInterval\">Interval (seconds):</label>
        <input id=\"rpsInterval\" type=\"number\" min=\"2\" max=\"120\" step=\"1\" value=\"10\" style=\"width:90px\" />
        <button id=\"rpsApply\">Apply Interval</button>
        <span class=\"meta\" id=\"rpsMsg\"></span>
      </div>
      <div class=\"meta\" id=\"rpsState\" style=\"margin-top:8px\">RPS: ...</div>
    </div>

    <div class=\"panel\" style=\"margin-top:16px\">
      <div class=\"row\">
        <strong>Container Conversation (Memcached simulation)</strong>
      </div>
      <div class=\"row\" style=\"margin-top:10px\">
        <input id=\"chatInput\" type=\"text\" maxlength=\"280\" placeholder=\"Say something to other containers...\" style=\"flex:1;min-width:240px\" />
        <button id=\"chatSend\">Send</button>
      </div>
      <div class=\"meta\" id=\"chatMsg\" style=\"margin-top:8px\"></div>
      <div id=\"chatFeed\" style=\"margin-top:10px; display:grid; gap:6px;\"></div>
    </div>
  </div>

  <script>
    const grid = document.getElementById('grid');
    const svc = document.getElementById('svc');
    const counts = document.getElementById('counts');
    const replicasInput = document.getElementById('replicas');
    const applyBtn = document.getElementById('apply');
    const msg = document.getElementById('msg');
    const chatInput = document.getElementById('chatInput');
    const chatSend = document.getElementById('chatSend');
    const chatMsg = document.getElementById('chatMsg');
    const chatFeed = document.getElementById('chatFeed');
    const rpsInterval = document.getElementById('rpsInterval');
    const rpsApply = document.getElementById('rpsApply');
    const rpsMsg = document.getElementById('rpsMsg');
    const rpsState = document.getElementById('rpsState');
    let pendingTarget = null;
    let currentDesired = null;
    let currentRunning = null;
    let isEditingReplicaInput = false;
    const TILE_TOGGLE_KEY = 'clawbucket.tileToggles';

    function loadTileToggles() {
      try {
        const raw = localStorage.getItem(TILE_TOGGLE_KEY);
        const parsed = raw ? JSON.parse(raw) : {};
        return (parsed && typeof parsed === 'object') ? parsed : {};
      } catch {
        return {};
      }
    }

    function saveTileToggles(toggles) {
      localStorage.setItem(TILE_TOGGLE_KEY, JSON.stringify(toggles));
    }

    function setTileToggle(taskId, isOn) {
      const toggles = loadTileToggles();
      toggles[taskId] = isOn;
      try {
        saveTileToggles(toggles);
      } catch (e) {
        // Ignore storage failures (private mode, disabled storage, quota)
      }
    }

    function applyTileVisualState(tileEl, isOn) {
      const badge = tileEl.querySelector('.status');
      if (badge) {
        badge.textContent = isOn ? 'ON' : 'OFF';
        badge.classList.toggle('on', isOn);
      }
      const armBtn = tileEl.querySelector('.arm-btn');
      if (armBtn) {
        armBtn.classList.toggle('on', isOn);
        armBtn.textContent = 'Arm';
      }
    }

    grid.addEventListener('click', (event) => {
      const armBtn = event.target.closest('.arm-btn');
      if (!armBtn || !grid.contains(armBtn)) return;
      const tile = armBtn.closest('.chip');
      if (!tile) return;
      const taskId = tile.dataset.taskId;
      const botName = tile.dataset.botName;
      if (!taskId) return;
      const nowOn = !armBtn.classList.contains('on');
      applyTileVisualState(tile, nowOn);
      setTileToggle(taskId, nowOn);

      fetch('/api/arm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_id: taskId,
          bot: botName,
          state: nowOn ? 'on' : 'off',
        }),
      }).catch(() => {});
    });


    replicasInput.addEventListener('focus', () => {
      isEditingReplicaInput = true;
    });

    replicasInput.addEventListener('blur', () => {
      isEditingReplicaInput = false;
    });

    // Prevent accidental mouse-wheel value changes on number input.
    replicasInput.addEventListener('wheel', (e) => {
      e.preventDefault();
    }, { passive: false });

    // Scaling should happen only by clicking the button.
    replicasInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') e.preventDefault();
    });

    function renderChat(messages) {
      const rows = (messages || []).slice(-12).reverse();
      chatFeed.innerHTML = '';
      for (const m of rows) {
        const row = document.createElement('div');
        row.className = 'meta';
        row.style.padding = '6px 8px';
        row.style.border = '1px solid #2b3a67';
        row.style.borderRadius = '8px';
        row.style.background = '#0f1730';
        row.textContent = `[${(m.at || '').slice(11, 19)}] ${m.from || 'unknown'}: ${m.text || ''}`;
        chatFeed.appendChild(row);
      }
      if (!rows.length) {
        chatFeed.innerHTML = '<div class="meta">No messages yet.</div>';
      }
    }

    async function loadChat() {
      try {
        const res = await fetch('/api/chat');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Chat unavailable');
        renderChat(data.messages || []);
      } catch (e) {
        chatMsg.textContent = e.message;
      }
    }

    async function sendChat() {
      const text = (chatInput.value || '').trim();
      if (!text) return;
      chatSend.disabled = true;
      chatMsg.textContent = 'Sending...';
      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Send failed');
        chatInput.value = '';
        chatMsg.textContent = 'Sent.';
        await loadChat();
      } catch (e) {
        chatMsg.textContent = e.message;
      } finally {
        chatSend.disabled = false;
      }
    }

    chatSend.addEventListener('click', sendChat);
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        sendChat();
      }
    });

    async function loadRps() {
      try {
        const res = await fetch('/api/rps');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'RPS unavailable');
        if (document.activeElement !== rpsInterval) {
          rpsInterval.value = data.interval_seconds;
        }
        const s = data.state;
        rpsState.textContent = s
          ? `RPS: ${String(s.choice || '').toUpperCase()} from ${s.from || 'unknown'} at ${s.at || 'unknown'} (TTL ${data.ttl_seconds}s)`
          : `RPS: waiting for first broadcast... (TTL ${data.ttl_seconds}s)`;
      } catch (e) {
        rpsMsg.textContent = e.message;
      }
    }

    rpsApply.addEventListener('click', async () => {
      const n = parseInt(rpsInterval.value, 10);
      if (!Number.isInteger(n) || n < 2 || n > 120) {
        rpsMsg.textContent = 'Interval must be 2-120 seconds';
        return;
      }
      rpsApply.disabled = true;
      rpsMsg.textContent = 'Saving...';
      try {
        const res = await fetch('/api/rps/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ interval_seconds: n }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to set interval');
        rpsMsg.textContent = `Interval set to ${data.interval_seconds}s`;
        await loadRps();
      } catch (e) {
        rpsMsg.textContent = e.message;
      } finally {
        rpsApply.disabled = false;
      }
    });

    async function loadState() {
      try {
        const res = await fetch('/api/swarm');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Unable to load swarm data');

        svc.textContent = `service: ${data.service}`;
        counts.textContent = `running: ${data.running_count} / desired: ${data.desired_replicas}`;
        currentDesired = data.desired_replicas;
        currentRunning = data.running_count;

        if (pendingTarget === null && !isEditingReplicaInput) {
          replicasInput.value = data.desired_replicas;
        }

        if (pendingTarget !== null) {
          applyBtn.disabled = true;
          replicasInput.disabled = true;
          if (data.desired_replicas === pendingTarget && data.running_count === pendingTarget) {
            pendingTarget = null;
            applyBtn.disabled = false;
            replicasInput.disabled = false;
            replicasInput.value = data.desired_replicas;
            msg.textContent = `Scale complete: ${data.running_count} / ${data.desired_replicas}`;
          } else {
            msg.textContent = `Scaling in progress... running ${data.running_count} / desired ${data.desired_replicas}`;
          }
        } else {
          applyBtn.disabled = false;
          replicasInput.disabled = false;
          msg.textContent = (data.running_count === data.desired_replicas)
            ? `Stable: ${data.running_count} / ${data.desired_replicas}`
            : `Reconciling: running ${data.running_count} / desired ${data.desired_replicas}`;
        }

        const tileToggles = loadTileToggles();
        grid.innerHTML = '';
        for (const r of data.replicas) {
          const el = document.createElement('div');
          const isOn = tileToggles[r.id] === true;
          el.className = `chip ${r.is_manager ? 'manager' : ''}`;
          el.dataset.taskId = r.id;
          el.dataset.botName = r.name;
          el.style.setProperty('--chip-color', r.color);
          const scoreClass = (r.score || 0) < 0 ? 'negative' : 'positive';
          const scoreText = (r.score || 0) > 0 ? `+${r.score}` : `${r.score || 0}`;
          el.innerHTML = `
            <span class="status">OFF</span>${r.is_manager ? '<span class="manager-badge">MANAGER</span>' : ''}
            <h3>${r.name}</h3>
            ${r.is_manager ? '' : `<div class="score ${scoreClass}">${scoreText}</div>`}
            <p><strong>Slot:</strong> ${r.slot}</p>
            <p><strong>Task:</strong> ${r.id.slice(0, 12)}</p>
            <p><strong>Node:</strong> ${r.node_id.slice(0, 12)}</p>
            <button type="button" class="arm-btn">Arm</button>
          `;
          applyTileVisualState(el, isOn);
          grid.appendChild(el);
        }
      } catch (e) {
        msg.textContent = e.message;
      }
    }

    applyBtn.addEventListener('click', async () => {
      const replicas = parseInt(replicasInput.value, 10);
      if (!Number.isInteger(replicas) || replicas < 1 || replicas > 25) {
        msg.textContent = 'Replicas must be a whole number between 1 and 25';
        return;
      }

      if (pendingTarget !== null) {
        msg.textContent = `Please wait — scaling to ${pendingTarget} in progress`;
        return;
      }

      if (currentDesired === replicas && currentRunning === replicas) {
        msg.textContent = `Already at ${replicas} replicas`;
        return;
      }

      pendingTarget = replicas;
      applyBtn.disabled = true;
      replicasInput.disabled = true;
      msg.textContent = `Scaling requested: target ${replicas}...`;

      async function postScaleOnce() {
        const res = await fetch('/api/scale', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ replicas }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Scale failed');
        return data;
      }

      try {
        await postScaleOnce();
        setTimeout(loadState, 1000);
      } catch (e) {
        // Routing mesh can briefly drop the first request while tasks are being replaced.
        try {
          await new Promise(r => setTimeout(r, 500));
          await postScaleOnce();
          setTimeout(loadState, 1000);
        } catch (e2) {
          pendingTarget = null;
          applyBtn.disabled = false;
          replicasInput.disabled = false;
          msg.textContent = e2.message || e.message;
        }
      }
    });

    loadState();
    loadChat();
    loadRps();
    setInterval(loadState, 3000);
    setInterval(loadChat, 4000);
    setInterval(loadRps, 3000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    Thread(target=heartbeat_loop, daemon=True).start()
    Thread(target=rps_loop, daemon=True).start()
    Thread(target=player_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
