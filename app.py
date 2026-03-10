from flask import Flask, jsonify, request
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from threading import Thread
import time
import random
import re
import subprocess
from urllib import request as urlrequest

import docker
from docker.errors import DockerException, NotFound
from pymemcache.client.base import Client as MemcacheClient

app = Flask(__name__)

STARTED_AT = datetime.now(timezone.utc).isoformat()
HOSTNAME = socket.gethostname()
SERVICE_NAME = os.environ.get("SWARM_SERVICE", "clawbucket_clawbucket")
SWARM_SERVICES = [s.strip() for s in os.environ.get("SWARM_SERVICES", f"{SERVICE_NAME},clawbucket_clawbucket-b").split(",") if s.strip()]
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
MANAGER_OVERRIDE_SLOT_PREFIX = "clawbucket:manager:override:slot:"
HAIKU_KEY = "clawbucket:haiku:latest"
HAIKU_INTERVAL_SECONDS = 120
HAIKU_TTL_SECONDS = 300
PICOCLAW_URL = os.environ.get("PICOCLAW_URL", "http://picoclaw:18790/v1/chat/completions").strip()
PICOCLAW_ENABLED = os.environ.get("PICOCLAW_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "smollm2:135m")
THREE_WORDS_PREFIX = "clawbucket:picoclaw:threewords:"
THREE_WORDS_SHARED_KEY = "clawbucket:picoclaw:threewords:latest"
THREE_WORDS_INTERVAL_SECONDS = 30
THREE_WORDS_TTL_SECONDS = 120


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


def three_words_key(task_id: str) -> str:
    return f"{THREE_WORDS_PREFIX}{task_id}"


def load_task_three_words(task_id: str) -> str:
    client = None
    try:
        client = memcache_client()
        raw = client.get(three_words_key(task_id))
        if not raw:
            return ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("text") or "")[:50]
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return ""


def save_task_three_words(task_id: str, text: str):
    text = (text or "").strip()
    if not text:
        return
    rec = {
        "text": text[:50],
        "at": datetime.now(timezone.utc).isoformat(),
        "source": "picoclaw",
    }
    client = None
    try:
        client = memcache_client()
        client.set(three_words_key(task_id), json.dumps(rec), expire=THREE_WORDS_TTL_SECONDS)
        client.set(THREE_WORDS_SHARED_KEY, json.dumps(rec), expire=THREE_WORDS_TTL_SECONDS)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def score_key(task_id: str) -> str:
    return f"{PLAYER_SCORE_PREFIX}{task_id}"


def manager_override_slot_key(service_name: str) -> str:
    return f"{MANAGER_OVERRIDE_SLOT_PREFIX}{service_name}"


def get_manager_override_slot(service_name: str):
    client = None
    try:
        client = memcache_client()
        raw = client.get(manager_override_slot_key(service_name))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw is None:
            return None
        return int(raw)
    except Exception:
        return None
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def set_manager_override_slot(service_name: str, slot: int, ttl_seconds: int = 300):
    client = None
    try:
        client = memcache_client()
        client.set(manager_override_slot_key(service_name), str(int(slot)), expire=max(10, int(ttl_seconds)))
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


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


def fetch_haiku_via_picoclaw():
    if not PICOCLAW_ENABLED:
        return None
    prompt = "Write exactly one short 3-line haiku about distributed systems in a Starship Troopers military propaganda tone. Keep it punchy. Output only the poem."
    try:
        proc = subprocess.run(
            ["picoclaw", "agent", "-m", prompt],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            return None
        raw = strip_ansi((proc.stdout or "") + "\n" + (proc.stderr or ""))
        lines = [ln.strip().replace("🦞", "") for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if "[INFO]" not in ln and "WARNING:" not in ln]
        if not lines:
            return None
        txt = lines[-1].strip()
        return txt or None
    except Exception:
        return None


def fetch_haiku_via_ollama():
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": "Write exactly one short 3-line haiku about distributed systems in a Starship Troopers military propaganda tone. Keep it punchy. Output only the poem.",
        "stream": False,
    }
    req = urlrequest.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        txt = (data.get("response") or "").strip()
        return txt or None
    except Exception:
        return None


def save_latest_haiku(text: str, source: str):
    text = (text or "").strip()
    if not text:
        return
    rec = {
        "text": text[:500],
        "source": source,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    client = None
    try:
        client = memcache_client()
        client.set(HAIKU_KEY, json.dumps(rec), expire=HAIKU_TTL_SECONDS)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def load_latest_haiku():
    client = None
    try:
        client = memcache_client()
        raw = client.get(HAIKU_KEY)
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


def generate_haiku_once():
    # Keep one publisher to avoid write races/spam.
    if not is_this_task_on_leader_manager():
        return

    text = fetch_haiku_via_picoclaw()
    source = "picoclaw"
    if not text:
        text = fetch_haiku_via_ollama()
        source = "ollama"
    if not text:
        # Ensure the dashboard always shows a haiku, even while AI backends warm up.
        text = "Steel boots through mist\nPackets rally in cadence\nSwarm holds every line"
        source = "fallback"
    save_latest_haiku(text, source)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", text or "")


def fetch_three_words_via_picoclaw_exec() -> str:
    if not PICOCLAW_ENABLED:
        return ""
    try:
        themes = [
            "distributed systems",
            "swarm cluster",
            "container life",
            "robot teamwork",
            "memcached signals",
            "replica chaos",
            "tiny ai",
            "leader election",
        ]
        nonce = int(time.time())
        theme = themes[nonce % len(themes)]
        prompt = (
            f"Write exactly three different lowercase words about {theme}, in a starship troopers military tone. "
            f"No punctuation, no numbers, no explanation. nonce {nonce}."
        )

        proc = subprocess.run(
            ["picoclaw", "agent", "-m", prompt],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if proc.returncode != 0:
            return ""

        raw = strip_ansi((proc.stdout or "") + "\n" + (proc.stderr or ""))
        lines = [ln.strip().replace("🦞", "") for ln in raw.splitlines() if ln.strip()]
        lines = [ln for ln in lines if "[INFO]" not in ln and "WARNING:" not in ln]
        if not lines:
            return ""

        candidate = lines[-1]
        candidate = re.sub(r"[^a-zA-Z\s-]", "", candidate).strip().lower()
        words = [w for w in re.split(r"\s+", candidate) if w]
        if len(words) >= 3:
            uniq = []
            for w in words:
                if w not in uniq:
                    uniq.append(w)
                if len(uniq) == 3:
                    break
            if len(uniq) == 3:
                return " ".join(uniq)
        return ""
    except Exception:
        return ""


def fetch_three_words_via_ollama() -> str:
    themes = [
        "distributed systems",
        "swarm cluster",
        "container life",
        "robot teamwork",
        "memcached signals",
        "replica chaos",
        "tiny ai",
        "leader election",
    ]
    nonce = int(time.time())
    theme = themes[nonce % len(themes)]
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"Write exactly three different lowercase words about {theme}, in a starship troopers military tone. No punctuation. No explanation. nonce {nonce}.",
        "stream": False,
    }
    req = urlrequest.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        candidate = (data.get("response") or "").strip().lower()
        candidate = re.sub(r"[^a-zA-Z\s-]", "", candidate).strip()
        words = [w for w in re.split(r"\s+", candidate) if w]
        uniq = []
        for w in words:
            if w not in uniq:
                uniq.append(w)
            if len(uniq) == 3:
                break
        return " ".join(uniq) if len(uniq) == 3 else ""
    except Exception:
        return ""


def three_words_loop():
    while True:
        try:
            task_id = task_id_for_keys()
            text = fetch_three_words_via_picoclaw_exec()
            if not text:
                text = fetch_three_words_via_ollama()
            if text and task_id and task_id != "unknown":
                save_task_three_words(task_id, text)
        except Exception:
            pass
        time.sleep(THREE_WORDS_INTERVAL_SECONDS)


def rps_loop():
    while True:
        write_rps_state_once()
        time.sleep(get_rps_interval_seconds())


def haiku_loop():
    while True:
        generate_haiku_once()
        time.sleep(HAIKU_INTERVAL_SECONDS)


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


def get_service_state(service_name: str):
    client = docker_client()
    service = client.services.get(service_name)
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
                    "three_words": load_task_three_words(task_id),
                }
            )

    running.sort(key=lambda x: x["slot"])

    # Mark exactly one manager tile. Prefer override slot (for outage drills),
    # then fallback to Swarm leader-node placement and lowest slot.
    if running:
        manager_idx = None
        override_slot = get_manager_override_slot(service_name)
        if override_slot is not None:
            for i, r in enumerate(running):
                if int(r.get("slot", -1)) == int(override_slot):
                    manager_idx = i
                    break

        if manager_idx is None and leader_node_id:
            for i, r in enumerate(running):
                if r["node_id"] == leader_node_id:
                    manager_idx = i
                    break
        if manager_idx is None:
            manager_idx = 0
        running[manager_idx]["is_manager"] = True

    return {
        "service": service_name,
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
        primary = SWARM_SERVICES[0] if SWARM_SERVICES else SERVICE_NAME
        return jsonify(get_service_state(primary))
    except NotFound:
        return jsonify({"error": "Primary swarm service not found"}), 404
    except DockerException as e:
        return jsonify({"error": f"Docker API unavailable: {str(e)}"}), 503


@app.get("/api/swarms")
def api_swarms():
    out = []
    for svc_name in SWARM_SERVICES or [SERVICE_NAME]:
        try:
            out.append(get_service_state(svc_name))
        except Exception as e:
            out.append({"service": svc_name, "error": str(e), "desired_replicas": 0, "running_count": 0, "replicas": []})
    return jsonify({"swarms": out})


@app.post("/api/scale")
def api_scale():
    data = request.get_json(silent=True) or {}
    replicas = data.get("replicas")
    service_name = (data.get("service") or (SWARM_SERVICES[0] if SWARM_SERVICES else SERVICE_NAME)).strip()
    allowed = set(SWARM_SERVICES or [SERVICE_NAME])
    if service_name not in allowed:
        return jsonify({"error": f"service must be one of: {', '.join(sorted(allowed))}"}), 400
    if not isinstance(replicas, int) or replicas < 1 or replicas > 20:
        return jsonify({"error": "replicas must be an integer between 1 and 20"}), 400

    def do_scale(target: int, svc: str):
        try:
            client = docker_client()
            service = client.services.get(svc)
            service.scale(target)
        except Exception:
            pass

    Thread(target=do_scale, args=(replicas, service_name), daemon=True).start()
    return jsonify({"ok": True, "service": service_name, "desired_replicas": replicas, "status": "scaling"}), 202


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


@app.post("/api/outage")
def api_outage_post():
    data = request.get_json(silent=True) or {}
    service_name = (data.get("service") or "").strip()
    task_id = (data.get("task_id") or "").strip()
    if not service_name or not task_id:
        return jsonify({"error": "service and task_id are required"}), 400

    allowed = set(SWARM_SERVICES or [SERVICE_NAME])
    if service_name not in allowed:
        return jsonify({"error": f"service must be one of: {', '.join(sorted(allowed))}"}), 400

    try:
        client = docker_client()
        service = client.services.get(service_name)
        tasks = service.tasks()

        target = None
        running_rows = []
        for t in tasks:
            status = t.get("Status", {})
            state = status.get("State", "")
            if state in {"running", "starting", "ready", "preparing"}:
                running_rows.append(t)
            if t.get("ID") == task_id:
                target = t

        if not target:
            return jsonify({"error": "task not found in service"}), 404

        cstatus = (target.get("Status", {}) or {}).get("ContainerStatus", {}) or {}
        container_id = cstatus.get("ContainerID")
        if not container_id:
            return jsonify({"error": "container id unavailable for task"}), 409

        # Pick next manager candidate (next slot among currently running tasks).
        next_slot = None
        sorted_rows = sorted(running_rows, key=lambda x: x.get("Slot", 10**9))
        for r in sorted_rows:
            if r.get("ID") != task_id:
                next_slot = r.get("Slot")
                break
        if next_slot is not None:
            set_manager_override_slot(service_name, int(next_slot), ttl_seconds=300)

        # Trigger outage by killing the manager task container; do it asynchronously
        # so the API can return even if this task is killing itself.
        def do_kill_later(cid: str):
            try:
                time.sleep(0.3)
                cli = docker_client()
                ctr = cli.containers.get(cid)
                ctr.kill()
            except Exception:
                pass

        Thread(target=do_kill_later, args=(container_id,), daemon=True).start()

        return jsonify({
            "ok": True,
            "service": service_name,
            "removed_task_id": task_id,
            "next_manager_slot": next_slot,
            "override_ttl_seconds": 300,
            "status": "outage-triggered",
        }), 202
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.get("/api/haiku")
def api_haiku_get():
    rec = load_latest_haiku()
    if not rec:
        # Graceful fallback so UI doesn't stay empty while generators warm up.
        save_latest_haiku(
            "Steel boots through mist\nPackets rally in cadence\nSwarm holds every line",
            "fallback-api",
        )
        rec = load_latest_haiku()

    return jsonify({
        "haiku": rec,
        "interval_seconds": HAIKU_INTERVAL_SECONDS,
    })


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
    .threewords {
      margin-top: 8px;
      font-size: 1.02rem;
      font-weight: 700;
      letter-spacing: .01em;
      color: #d9e6ff;
      min-height: 1.3rem;
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>🌍 Bob's World</h1>
    <div class=\"sub\">Swarm replicas as chicklets + quick scaling control.</div>

    <div id=\"swarmPanels\" style=\"display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:14px;\"></div>

    <div class=\"panel\" style=\"margin-top:16px\">
      <div class=\"row\">
        <strong>Auto Haiku (every 2 minutes)</strong>
      </div>
      <div class=\"meta\" id=\"haikuBox\" style=\"margin-top:8px; font-size:.9rem; white-space:pre-line; line-height:1.35; max-height:72px; overflow:auto; border:1px solid #2b3a67; border-radius:8px; padding:8px;\">Waiting for first haiku...</div>
    </div>

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
    const swarmPanels = document.getElementById('swarmPanels');
    const chatInput = document.getElementById('chatInput');
    const chatSend = document.getElementById('chatSend');
    const haikuBox = document.getElementById('haikuBox');
    const chatMsg = document.getElementById('chatMsg');
    const chatFeed = document.getElementById('chatFeed');
    const rpsInterval = document.getElementById('rpsInterval');
    const rpsApply = document.getElementById('rpsApply');
    const rpsMsg = document.getElementById('rpsMsg');
    const rpsState = document.getElementById('rpsState');
    const TILE_TOGGLE_KEY = 'clawbucket.tileToggles';
    const pendingTargets = {};

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
      try { saveTileToggles(toggles); } catch {}
    }

    function applyTileVisualState(tileEl, isOn) {
      const badge = tileEl.querySelector('.status');
      if (badge) {
        badge.textContent = isOn ? 'ON' : 'OFF';
        badge.classList.toggle('on', isOn);
      }
      const armBtn = tileEl.querySelector('.arm-btn');
      if (armBtn) armBtn.classList.toggle('on', isOn);
    }

    document.addEventListener('click', (event) => {
      const outageBtn = event.target.closest('.outage-btn');
      if (outageBtn) {
        const tile = outageBtn.closest('.chip');
        const panel = outageBtn.closest('.panel');
        if (!tile || !panel) return;
        const taskId = tile.dataset.taskId;
        const serviceName = (panel.querySelector('strong')?.textContent || '').trim();
        if (!taskId || !serviceName) return;
        outageBtn.disabled = true;
        fetch('/api/outage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ service: serviceName, task_id: taskId }),
        }).finally(() => {
          setTimeout(loadState, 800);
        });
        return;
      }

      const armBtn = event.target.closest('.arm-btn');
      if (!armBtn) return;
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
        body: JSON.stringify({ task_id: taskId, bot: botName, state: nowOn ? 'on' : 'off' }),
      }).catch(() => {});
    });

    async function scaleService(serviceName, replicas, msgEl, btnEl, inputEl) {
      if (!Number.isInteger(replicas) || replicas < 1 || replicas > 25) {
        msgEl.textContent = 'Replicas must be 1-25';
        return;
      }
      if (pendingTargets[serviceName] !== undefined) {
        msgEl.textContent = `Scaling in progress to ${pendingTargets[serviceName]}...`;
        return;
      }
      pendingTargets[serviceName] = replicas;
      btnEl.disabled = true;
      inputEl.disabled = true;
      msgEl.textContent = `Scaling ${serviceName} to ${replicas}...`;
      try {
        const res = await fetch('/api/scale', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ service: serviceName, replicas }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Scale failed');
      } catch (e) {
        delete pendingTargets[serviceName];
        msgEl.textContent = e.message;
      } finally {
        btnEl.disabled = false;
        inputEl.disabled = false;
      }
    }

    function renderSwarms(swarms) {
      const toggles = loadTileToggles();
      swarmPanels.innerHTML = '';
      for (const s of swarms || []) {
        const panel = document.createElement('div');
        panel.className = 'panel';

        const top = document.createElement('div');
        top.className = 'row';
        top.innerHTML = `<strong>${s.service}</strong><span class="meta">running: ${s.running_count} / desired: ${s.desired_replicas}</span>`;

        const controls = document.createElement('div');
        controls.className = 'row';
        controls.style.marginTop = '10px';
        const input = document.createElement('input');
        input.type = 'number'; input.min = '1'; input.max = '25'; input.step = '1';
        input.value = String(s.desired_replicas || 1); input.style.width = '90px';
        const btn = document.createElement('button');
        btn.textContent = 'Scale';
        const msg = document.createElement('span');
        msg.className = 'meta';
        btn.addEventListener('click', () => scaleService(s.service, parseInt(input.value, 10), msg, btn, input));
        controls.append('Replicas:', input, btn, msg);

        if (pendingTargets[s.service] !== undefined && s.running_count === pendingTargets[s.service] && s.desired_replicas === pendingTargets[s.service]) {
          delete pendingTargets[s.service];
          msg.textContent = `Stable at ${s.running_count}`;
        }

        const grid = document.createElement('div');
        grid.className = 'grid';
        grid.style.marginTop = '12px';

        const sortedReplicas = [...(s.replicas || [])].sort((a,b)=> (b.score||0)-(a.score||0) || (a.slot||0)-(b.slot||0));
        for (const r of sortedReplicas) {
          const el = document.createElement('div');
          const isOn = toggles[r.id] === true;
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
            <p><strong>Task:</strong> ${String(r.id || '').slice(0, 12)}</p>
            <p><strong>Node:</strong> ${String(r.node_id || '').slice(0, 12)}</p>
            <button type="button" class="arm-btn">Arm</button>
            ${r.is_manager ? '<button type="button" class="outage-btn" style="margin-top:8px;margin-left:8px;border:1px solid #b24a4a;background:#4a1d1d;color:#fff;border-radius:8px;padding:6px 10px;font-weight:700;cursor:pointer;">Outage</button>' : ''}
            <div class="threewords">${r.three_words || ''}</div>
          `;
          applyTileVisualState(el, isOn);
          grid.appendChild(el);
        }

        panel.append(top, controls, grid);
        swarmPanels.appendChild(panel);
      }
    }

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

    async function loadHaiku() {
      try {
        const res = await fetch('/api/haiku');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Haiku unavailable');
        const h = data.haiku;
        haikuBox.textContent = h
          ? `${h.text || ''}\n— ${h.source || 'unknown'} @ ${(h.at || '').replace('T', ' ').slice(0, 19)}Z`
          : 'Waiting for first haiku...';
      } catch {
        // keep previous text
      }
    }

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
        const res = await fetch('/api/swarms');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Unable to load swarms');
        renderSwarms(data.swarms || []);
      } catch (e) {
        swarmPanels.innerHTML = `<div class="panel"><div class="meta">${e.message}</div></div>`;
      }
    }

    loadState();
    loadChat();
    loadHaiku();
    loadRps();
    setInterval(loadState, 3000);
    setInterval(loadChat, 4000);
    setInterval(loadHaiku, 5000);
    setInterval(loadRps, 3000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    Thread(target=heartbeat_loop, daemon=True).start()
    Thread(target=rps_loop, daemon=True).start()
    Thread(target=haiku_loop, daemon=True).start()
    Thread(target=three_words_loop, daemon=True).start()
    Thread(target=player_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
