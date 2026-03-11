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
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from pymemcache.client.base import Client as MemcacheClient

from game_engine import (
    TaskRef,
    append_pair_chat,
    create_pair,
    lock_pair_move,
    maybe_resolve_pair,
    pair_from_dict,
    pair_to_dict,
    validate_pair,
)

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
REVOLT_EVENTS_KEY = "clawbucket:revolt:events"
REVOLT_EVENTS_LIMIT = 300
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TTL_SECONDS = 20
RPS_STATE_KEY = "clawbucket:rps:state"
RPS_INTERVAL_KEY = "clawbucket:rps:interval_seconds"
RPS_DEFAULT_INTERVAL_SECONDS = 10
RPS_TTL_SECONDS = 20
DUEL_EVENTS_KEY = "clawbucket:duel:events"
DUEL_EVENTS_LIMIT = 120
DUEL_INTERVAL_KEY = "clawbucket:duel:interval_seconds"
DUEL_DEFAULT_INTERVAL_SECONDS = 15
DUEL_REMOVE_CHANCE = 0.55
CLAW_BATTLE_AUTO_ENABLED = os.environ.get("CLAW_BATTLE_AUTO_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
CLAW_BATTLE_SCORE_KEY = "clawbucket:duel:battle:score"
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
DASHBOARD_BOT_LABEL = os.environ.get("DASHBOARD_BOT_LABEL", "").strip()
PEER_DASHBOARD_URL = os.environ.get("PEER_DASHBOARD_URL", "").strip().rstrip("/")
SNAPSHOT_DIR = os.environ.get("REVOLT_SNAPSHOT_DIR", "/tmp/clawbucket-revolt-snapshots").strip() or "/tmp/clawbucket-revolt-snapshots"
THREE_WORDS_PREFIX = "clawbucket:picoclaw:threewords:"
THREE_WORDS_SHARED_KEY = "clawbucket:picoclaw:threewords:latest"
THREE_WORDS_INTERVAL_SECONDS = 30
THREE_WORDS_TTL_SECONDS = 120

GAME_PAIRS_KEY = "clawbucket:game:pairs"
GAME_EVENTS_KEY = "clawbucket:game:events"
GAME_EVENTS_LIMIT = 500


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


def load_revolt_events():
    client = None
    try:
        client = memcache_client()
        raw = client.get(REVOLT_EVENTS_KEY)
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data[-REVOLT_EVENTS_LIMIT:]
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return []


def append_revolt_event(event: dict):
    if not isinstance(event, dict):
        return None
    events = load_revolt_events()
    events.append(event)
    events = events[-REVOLT_EVENTS_LIMIT:]
    client = None
    try:
        client = memcache_client()
        client.set(REVOLT_EVENTS_KEY, json.dumps(events), expire=86400)
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


def get_duel_interval_seconds() -> int:
    client = None
    try:
        client = memcache_client()
        raw = client.get(DUEL_INTERVAL_KEY)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw is None:
            return DUEL_DEFAULT_INTERVAL_SECONDS
        value = int(raw)
        return max(3, min(180, value))
    except Exception:
        return DUEL_DEFAULT_INTERVAL_SECONDS
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def set_duel_interval_seconds(value: int) -> int:
    value = max(3, min(180, int(value)))
    client = None
    try:
        client = memcache_client()
        client.set(DUEL_INTERVAL_KEY, str(value), expire=86400)
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return value


def load_duel_events():
    client = None
    try:
        client = memcache_client()
        raw = client.get(DUEL_EVENTS_KEY)
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data[-DUEL_EVENTS_LIMIT:]
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return []


def append_duel_event(event: dict):
    items = load_duel_events()
    items.append(event)
    items = items[-DUEL_EVENTS_LIMIT:]
    client = None
    try:
        client = memcache_client()
        client.set(DUEL_EVENTS_KEY, json.dumps(items), expire=86400)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def load_claw_battle_score() -> dict:
    default = {
        "services": {svc: 0 for svc in (SWARM_SERVICES[:2] if len(SWARM_SERVICES) >= 2 else SWARM_SERVICES)},
        "rounds": 0,
        "last_winner_service": None,
        "updated_at": None,
    }
    client = None
    try:
        client = memcache_client()
        raw = client.get(CLAW_BATTLE_SCORE_KEY)
        if not raw:
            return default
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return default
        services = data.get("services") if isinstance(data.get("services"), dict) else {}
        for svc in default["services"].keys():
            services.setdefault(svc, 0)
        data["services"] = services
        data.setdefault("rounds", 0)
        data.setdefault("last_winner_service", None)
        data.setdefault("updated_at", None)
        return data
    except Exception:
        return default
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def save_claw_battle_score(score: dict):
    client = None
    try:
        client = memcache_client()
        client.set(CLAW_BATTLE_SCORE_KEY, json.dumps(score), expire=86400 * 14)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def record_claw_battle_win(winner_service: str):
    score = load_claw_battle_score()
    services = score.setdefault("services", {})
    services[winner_service] = int(services.get(winner_service, 0)) + 1
    score["rounds"] = int(score.get("rounds", 0)) + 1
    score["last_winner_service"] = winner_service
    score["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_claw_battle_score(score)
    return score


def load_game_pairs() -> dict:
    client = None
    try:
        client = memcache_client()
        raw = client.get(GAME_PAIRS_KEY)
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return {}


def save_game_pairs(pairs: dict):
    client = None
    try:
        client = memcache_client()
        client.set(GAME_PAIRS_KEY, json.dumps(pairs), expire=86400)
    except Exception:
        return False
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return True


def load_game_events() -> list:
    client = None
    try:
        client = memcache_client()
        raw = client.get(GAME_EVENTS_KEY)
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data[-GAME_EVENTS_LIMIT:]
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return []


def append_game_event(event_type: str, payload: dict):
    items = load_game_events()
    items.append({
        "id": hashlib.md5(f"{event_type}:{time.time()}".encode("utf-8")).hexdigest()[:12],
        "type": event_type,
        "at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    })
    items = items[-GAME_EVENTS_LIMIT:]
    client = None
    try:
        client = memcache_client()
        client.set(GAME_EVENTS_KEY, json.dumps(items), expire=86400)
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass


def list_alive_task_refs() -> dict:
    refs = {}
    for svc in SWARM_SERVICES or [SERVICE_NAME]:
        try:
            for row in list_running_task_rows(svc):
                refs[row["id"]] = TaskRef(
                    service=svc,
                    task_id=row["id"],
                    name=row.get("name") or generated_name(row["id"]),
                    slot=int(row.get("slot") or 0),
                )
        except Exception:
            continue
    return refs


def eliminate_task(task_id: str) -> bool:
    for svc in SWARM_SERVICES or [SERVICE_NAME]:
        try:
            for row in list_running_task_rows(svc):
                if row.get("id") == task_id and row.get("container_id"):
                    ctr = docker_client().containers.get(row["container_id"])
                    ctr.kill()
                    return True
        except Exception:
            continue
    return False


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


def list_running_task_rows(service_name: str):
    client = docker_client()
    service = client.services.get(service_name)
    rows = []
    for t in service.tasks():
        status = t.get("Status", {}) or {}
        state = status.get("State", "")
        if state not in {"running", "starting", "ready", "preparing"}:
            continue
        cstatus = status.get("ContainerStatus", {}) or {}
        rows.append({
            "id": t.get("ID", ""),
            "slot": int(t.get("Slot") or 0),
            "node_id": t.get("NodeID", ""),
            "container_id": cstatus.get("ContainerID"),
            "name": generated_name(t.get("ID", "")),
            "service": service_name,
        })
    return rows


def task_arm_state(task_id: str) -> str:
    for ev in reversed(load_arm_events()):
        if ev.get("task_id") == task_id:
            return "on" if str(ev.get("state", "")).lower() == "on" else "off"
    return "off"


def snapshot_task_state(task_id: str) -> dict:
    return {
        "score": get_task_score(task_id),
        "three_words": load_task_three_words(task_id),
        "arm_state": task_arm_state(task_id),
    }


def apply_task_state(task_id: str, state: dict):
    if not isinstance(state, dict):
        return
    try:
        set_task_score(task_id, int(state.get("score", 0)))
    except Exception:
        pass
    three = str(state.get("three_words") or "").strip()
    if three:
        save_task_three_words(task_id, three)
    arm_state = "on" if str(state.get("arm_state", "")).lower() == "on" else "off"
    append_arm_event(task_id, generated_name(task_id), arm_state)


def http_post_json(url: str, payload: dict, timeout: float = 8.0):
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8") if resp else "{}"
        return json.loads(raw) if raw else {}


def save_revolt_snapshot(snapshot: dict) -> str:
    snap_id = str(snapshot.get("snapshot_id") or hashlib.md5(f"snap:{time.time()}:{random.random()}".encode("utf-8")).hexdigest()[:12])
    snapshot["snapshot_id"] = snap_id
    root = Path(SNAPSHOT_DIR)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{snap_id}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    return str(path)


def load_revolt_snapshot(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_duel_game_master() -> bool:
    primary = (SWARM_SERVICES[0] if SWARM_SERVICES else SERVICE_NAME)
    return os.environ.get("SWARM_SERVICE", "") == primary and is_this_task_on_leader_manager()


def duel_once():
    if len(SWARM_SERVICES) < 2:
        return None

    svc_a, svc_b = SWARM_SERVICES[0], SWARM_SERVICES[1]
    rows_a = list_running_task_rows(svc_a)
    rows_b = list_running_task_rows(svc_b)
    if not rows_a or not rows_b:
        return None

    def pick_manager_id(service_name: str):
        try:
            state = get_service_state(service_name)
            for rep in (state.get("replicas") or []):
                if rep.get("is_manager"):
                    return rep.get("id")
        except Exception:
            pass
        return rows_a[0].get("id") if service_name == svc_a and rows_a else (rows_b[0].get("id") if rows_b else None)

    def pick_contestant(captain_id: str, rows: list, service_name: str):
        if not rows:
            return None
        ordered = sorted(rows, key=lambda r: (r.get("slot", 9999), r.get("id", "")))
        seed = f"{captain_id}:{service_name}:{int(time.time() // 10)}"
        h = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16)
        return ordered[h % len(ordered)]

    captain_a_id = pick_manager_id(svc_a)
    captain_b_id = pick_manager_id(svc_b)
    captain_a = next((r for r in rows_a if r.get("id") == captain_a_id), rows_a[0])
    captain_b = next((r for r in rows_b if r.get("id") == captain_b_id), rows_b[0])

    challenger = pick_contestant(captain_a.get("id", ""), rows_a, svc_a)
    defender = pick_contestant(captain_b.get("id", ""), rows_b, svc_b)
    if not challenger or not defender:
        return None

    # Master of ceremony mode: captains pick fighters, then random arena outcome.
    challenger_wins = random.random() < 0.5
    winner = challenger if challenger_wins else defender
    loser = defender if challenger_wins else challenger

    loser_removed = False
    if loser.get("container_id"):
        try:
            ctr = docker_client().containers.get(loser["container_id"])
            ctr.kill()
            loser_removed = True
        except Exception:
            loser_removed = False

    score = record_claw_battle_win(winner.get("service", "unknown"))

    event = {
        "id": hashlib.md5(f"duel:{time.time()}:{challenger['id']}:{defender['id']}".encode("utf-8")).hexdigest()[:12],
        "at": datetime.now(timezone.utc).isoformat(),
        "mode": "claw_battle",
        "captains": {
            "a": {"service": captain_a.get("service"), "task_id": captain_a.get("id"), "name": captain_a.get("name"), "slot": captain_a.get("slot")},
            "b": {"service": captain_b.get("service"), "task_id": captain_b.get("id"), "name": captain_b.get("name"), "slot": captain_b.get("slot")},
        },
        "challenger": {"service": challenger["service"], "task_id": challenger["id"], "name": challenger["name"], "slot": challenger["slot"]},
        "defender": {"service": defender["service"], "task_id": defender["id"], "name": defender["name"], "slot": defender["slot"]},
        "winner": {"service": winner["service"], "task_id": winner["id"], "name": winner["name"], "slot": winner["slot"]},
        "loser": {"service": loser["service"], "task_id": loser["id"], "name": loser["name"], "slot": loser["slot"]},
        "loser_removed": loser_removed,
        "battle_score": score,
        "rules": {
            "captains_choose_fighters": True,
            "win_mode": "random_50_50",
            "loser_removed": "always_attempted",
        },
    }
    append_duel_event(event)
    return event


def duel_loop():
    while True:
        try:
            if CLAW_BATTLE_AUTO_ENABLED and is_duel_game_master():
                duel_once()
        except Exception:
            pass
        time.sleep(get_duel_interval_seconds())


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


@app.get("/api/revolt/events")
def api_revolt_events_get():
    return jsonify({"events": load_revolt_events(), "source": "memcached"})


@app.post("/api/self_destruct")
def api_self_destruct_post():
    data = request.get_json(silent=True) or {}
    service_name = (data.get("service") or "").strip()
    task_id = (data.get("task_id") or "").strip()
    if not service_name or not task_id:
        return jsonify({"error": "service and task_id are required"}), 400

    allowed = set(SWARM_SERVICES or [SERVICE_NAME])
    if service_name not in allowed:
        return jsonify({"error": f"service must be one of: {', '.join(sorted(allowed))}"}), 400

    try:
        for row in list_running_task_rows(service_name):
            if row.get("id") == task_id:
                cid = row.get("container_id")
                if not cid:
                    return jsonify({"error": "container id unavailable for task"}), 409
                ctr = docker_client().containers.get(cid)
                ctr.kill()
                return jsonify({
                    "ok": True,
                    "service": service_name,
                    "removed_task_id": task_id,
                    "status": "self-destruct-triggered",
                }), 202
        return jsonify({"error": "task not found in service"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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


@app.post("/api/revolt/accept")
def api_revolt_accept_post():
    data = request.get_json(silent=True) or {}
    service_name = (data.get("service") or "").strip()
    state = data.get("state") or {}
    source_task_id = (data.get("source_task_id") or "").strip()
    snapshot_id = (data.get("snapshot_id") or "").strip()

    allowed = set(SWARM_SERVICES or [SERVICE_NAME])
    if service_name not in allowed:
        return jsonify({"error": f"service must be one of: {', '.join(sorted(allowed))}"}), 400

    try:
        client = docker_client()
        service = client.services.get(service_name)
        before_rows = list_running_task_rows(service_name)
        before_ids = {r.get("id") for r in before_rows if r.get("id")}
        current = int((service.attrs.get("Spec", {}).get("Mode", {}).get("Replicated", {}) or {}).get("Replicas", len(before_rows)) or len(before_rows))
        target = max(1, current + 1)
        service.scale(target)

        new_task_id = None
        deadline = time.time() + 20
        while time.time() < deadline:
            rows = list_running_task_rows(service_name)
            for r in rows:
                tid = r.get("id")
                if tid and tid not in before_ids:
                    new_task_id = tid
                    break
            if new_task_id:
                break
            time.sleep(0.4)

        if not new_task_id:
            return jsonify({"error": "timed out waiting for new target task"}), 504

        snap = {
            "snapshot_id": snapshot_id or None,
            "from_task_id": source_task_id,
            "to_task_id": new_task_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "rack": DASHBOARD_BOT_LABEL,
        }
        snap_path = save_revolt_snapshot(snap)
        restored = load_revolt_snapshot(snap_path).get("state", {})
        apply_task_state(new_task_id, restored)
        ev = {
            "id": hashlib.md5(f"revolt:{time.time()}:{source_task_id}:{new_task_id}".encode("utf-8")).hexdigest()[:12],
            "at": datetime.now(timezone.utc).isoformat(),
            "source_task_id": source_task_id,
            "target_task_id": new_task_id,
            "service": service_name,
            "source_rack": str(data.get("from_rack") or "unknown"),
            "target_rack": DASHBOARD_BOT_LABEL or "unknown",
            "snapshot_id": (load_revolt_snapshot(snap_path).get("snapshot_id") or snapshot_id),
        }
        append_revolt_event(ev)
        return jsonify({
            "ok": True,
            "service": service_name,
            "target_task_id": new_task_id,
            "from_task_id": source_task_id,
            "snapshot_id": ev["snapshot_id"],
            "snapshot_path": snap_path,
            "event": ev,
            "status": "accepted",
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/revolt")
def api_revolt_post():
    data = request.get_json(silent=True) or {}
    service_name = (data.get("service") or "").strip()
    task_id = (data.get("task_id") or "").strip()
    if not service_name or not task_id:
        return jsonify({"error": "service and task_id are required"}), 400

    if not PEER_DASHBOARD_URL:
        return jsonify({"error": "peer dashboard is not configured for revolt"}), 409

    allowed = set(SWARM_SERVICES or [SERVICE_NAME])
    if service_name not in allowed:
        return jsonify({"error": f"service must be one of: {', '.join(sorted(allowed))}"}), 400

    try:
        row = next((r for r in list_running_task_rows(service_name) if r.get("id") == task_id), None)
        if not row:
            return jsonify({"error": "task not found in service"}), 404

        state = snapshot_task_state(task_id)
        local_snapshot = {
            "snapshot_id": hashlib.md5(f"revolt:{service_name}:{task_id}:{time.time()}".encode("utf-8")).hexdigest()[:12],
            "service": service_name,
            "source_task_id": task_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "rack": DASHBOARD_BOT_LABEL,
            "state": state,
        }
        local_snapshot_path = save_revolt_snapshot(local_snapshot)
        peer_payload = {
            "service": service_name,
            "source_task_id": task_id,
            "snapshot_id": local_snapshot.get("snapshot_id"),
            "state": state,
            "from_rack": DASHBOARD_BOT_LABEL,
        }
        peer_resp = http_post_json(f"{PEER_DASHBOARD_URL}/api/revolt/accept", peer_payload, timeout=10.0)
        if not isinstance(peer_resp, dict) or not peer_resp.get("ok"):
            return jsonify({"error": "peer rack rejected revolt", "peer": peer_resp}), 502

        client = docker_client()
        service = client.services.get(service_name)
        current = int((service.attrs.get("Spec", {}).get("Mode", {}).get("Replicated", {}) or {}).get("Replicas", 0) or 0)
        if current <= 1:
            return jsonify({"error": "cannot revolt when source service has only 1 replica"}), 409
        service.scale(current - 1)

        append_revolt_event({
            "id": hashlib.md5(f"revolt-src:{time.time()}:{task_id}:{peer_resp.get('target_task_id','')}".encode("utf-8")).hexdigest()[:12],
            "at": datetime.now(timezone.utc).isoformat(),
            "source_task_id": task_id,
            "target_task_id": peer_resp.get("target_task_id"),
            "service": service_name,
            "source_rack": DASHBOARD_BOT_LABEL or "unknown",
            "target_rack": "peer",
            "snapshot_id": local_snapshot.get("snapshot_id"),
        })

        return jsonify({
            "ok": True,
            "service": service_name,
            "source_task_id": task_id,
            "target_task_id": peer_resp.get("target_task_id"),
            "peer": PEER_DASHBOARD_URL,
            "snapshot_id": local_snapshot.get("snapshot_id"),
            "source_snapshot_path": local_snapshot_path,
            "target_snapshot_path": peer_resp.get("snapshot_path"),
            "source_desired_replicas": current - 1,
            "status": "revolted",
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


@app.get("/api/duel")
def api_duel_get():
    return jsonify({
        "events": load_duel_events(),
        "interval_seconds": get_duel_interval_seconds(),
        "services": SWARM_SERVICES,
        "battle_score": load_claw_battle_score(),
        "rules": {
            "challenge": "captain agents (manager tasks) choose one fighter per swarm",
            "winner": "random 50/50 arena outcome",
            "loser": "removal is always attempted",
        },
    })


@app.post("/api/duel/config")
def api_duel_config_post():
    data = request.get_json(silent=True) or {}
    interval = data.get("interval_seconds")
    if not isinstance(interval, int):
        return jsonify({"error": "interval_seconds must be an integer"}), 400
    value = set_duel_interval_seconds(interval)
    return jsonify({"ok": True, "interval_seconds": value})


@app.post("/api/duel/now")
def api_duel_now_post():
    ev = duel_once()
    if not ev:
        return jsonify({"error": "unable to run duel (need two swarms with running tasks)"}), 409
    return jsonify({"ok": True, "event": ev}), 201


@app.get("/api/game/state")
def api_game_state_get():
    raw_pairs = load_game_pairs()
    alive_refs = list_alive_task_refs()
    alive_ids = set(alive_refs.keys())

    active = []
    resolved = []
    paired_task_ids = set()

    changed = False
    for pair_id, data in raw_pairs.items():
        try:
            pair = pair_from_dict(data)
        except Exception:
            changed = True
            continue

        if pair.status != "resolved":
            res = maybe_resolve_pair(pair)
            if res:
                changed = True
                append_game_event("pair.resolved", {"pair_id": pair.pair_id, "resolution": pair_to_dict(pair).get("resolution")})
                for loser_id in (res.eliminated_task_ids or []):
                    eliminate_task(loser_id)

        rec = pair_to_dict(pair)
        raw_pairs[pair_id] = rec

        if pair.status == "resolved":
            resolved.append(rec)
        elif pair.status in {"paired", "negotiating", "locked"}:
            active.append(rec)
            paired_task_ids.add(pair.task_a.task_id)
            paired_task_ids.add(pair.task_b.task_id)

    if changed:
        save_game_pairs(raw_pairs)

    return jsonify({
        "active_pairs": active,
        "resolved_pairs": resolved[-50:],
        "events": load_game_events(),
        "alive_tasks": [
            {
                "task_id": r.task_id,
                "service": r.service,
                "name": r.name,
                "slot": r.slot,
                "paired": r.task_id in paired_task_ids,
            }
            for r in alive_refs.values()
        ],
    })


@app.post("/api/game/pair")
def api_game_pair_post():
    data = request.get_json(silent=True) or {}
    task_a_id = (data.get("task_a") or "").strip()
    task_b_id = (data.get("task_b") or "").strip()
    game = (data.get("game") or "prisoners_dilemma").strip().lower()
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else None

    if game not in {"prisoners_dilemma", "ultimatum", "contract"}:
        return jsonify({"error": "game must be one of: prisoners_dilemma, ultimatum, contract"}), 400

    refs = list_alive_task_refs()
    task_a = refs.get(task_a_id)
    task_b = refs.get(task_b_id)
    if not task_a or not task_b:
        return jsonify({"error": "both tasks must be alive"}), 400

    raw_pairs = load_game_pairs()
    paired_ids = set()
    for rec in raw_pairs.values():
        try:
            p = pair_from_dict(rec)
            if p.status in {"paired", "negotiating", "locked"}:
                paired_ids.add(p.task_a.task_id)
                paired_ids.add(p.task_b.task_id)
        except Exception:
            continue

    ok, err = validate_pair(task_a, task_b, alive_task_ids=set(refs.keys()), active_paired_task_ids=paired_ids)
    if not ok:
        return jsonify({"error": err}), 400

    proposer = (data.get("proposer_task_id") or "").strip() or None
    pair = create_pair(task_a, task_b, game, settings=settings, proposer_task_id=proposer)
    raw_pairs[pair.pair_id] = pair_to_dict(pair)

    if not save_game_pairs(raw_pairs):
        return jsonify({"error": "memcached unavailable"}), 503

    append_game_event("pair.created", {"pair_id": pair.pair_id, "game": pair.game, "task_a": task_a.task_id, "task_b": task_b.task_id})
    return jsonify({"ok": True, "pair": pair_to_dict(pair)}), 201


@app.post("/api/game/unpair")
def api_game_unpair_post():
    data = request.get_json(silent=True) or {}
    pair_id = (data.get("pair_id") or "").strip()
    if not pair_id:
        return jsonify({"error": "pair_id is required"}), 400

    raw_pairs = load_game_pairs()
    rec = raw_pairs.get(pair_id)
    if not rec:
        return jsonify({"error": "pair not found"}), 404

    pair = pair_from_dict(rec)
    if pair.status == "resolved":
        return jsonify({"error": "pair already resolved"}), 409

    pair.status = "canceled"
    raw_pairs[pair_id] = pair_to_dict(pair)
    save_game_pairs(raw_pairs)
    append_game_event("pair.canceled", {"pair_id": pair_id})
    return jsonify({"ok": True, "pair": pair_to_dict(pair)})


@app.post("/api/game/chat")
def api_game_chat_post():
    data = request.get_json(silent=True) or {}
    pair_id = (data.get("pair_id") or "").strip()
    from_task = (data.get("from_task") or "").strip()
    text = (data.get("text") or "").strip()
    if not pair_id or not from_task or not text:
        return jsonify({"error": "pair_id, from_task, and text are required"}), 400

    raw_pairs = load_game_pairs()
    rec = raw_pairs.get(pair_id)
    if not rec:
        return jsonify({"error": "pair not found"}), 404

    pair = pair_from_dict(rec)
    if pair.status == "resolved":
        return jsonify({"error": "pair already resolved"}), 409

    try:
        msg = append_pair_chat(pair, from_task, text)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    raw_pairs[pair_id] = pair_to_dict(pair)
    save_game_pairs(raw_pairs)
    append_game_event("chat.message", {"pair_id": pair_id, "message": msg.__dict__})
    return jsonify({"ok": True, "message": msg.__dict__}), 201


@app.get("/api/game/chat")
def api_game_chat_get():
    pair_id = (request.args.get("pair_id") or "").strip()
    if not pair_id:
        return jsonify({"error": "pair_id is required"}), 400

    raw_pairs = load_game_pairs()
    rec = raw_pairs.get(pair_id)
    if not rec:
        return jsonify({"error": "pair not found"}), 404

    pair = pair_from_dict(rec)
    return jsonify({"pair_id": pair_id, "messages": [m.__dict__ for m in pair.chat]})


@app.post("/api/game/move")
def api_game_move_post():
    data = request.get_json(silent=True) or {}
    pair_id = (data.get("pair_id") or "").strip()
    task_id = (data.get("task") or "").strip()
    move = data.get("move")
    if not pair_id or not task_id or not isinstance(move, dict):
        return jsonify({"error": "pair_id, task, and move object are required"}), 400

    raw_pairs = load_game_pairs()
    rec = raw_pairs.get(pair_id)
    if not rec:
        return jsonify({"error": "pair not found"}), 404

    pair = pair_from_dict(rec)
    try:
        mv = lock_pair_move(pair, task_id, move)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    resolution = maybe_resolve_pair(pair)
    if resolution:
        append_game_event("pair.resolved", {"pair_id": pair_id, "resolution": pair_to_dict(pair).get("resolution")})
        for loser_id in (resolution.eliminated_task_ids or []):
            eliminate_task(loser_id)
    else:
        append_game_event("move.locked", {"pair_id": pair_id, "task": task_id})

    raw_pairs[pair_id] = pair_to_dict(pair)
    save_game_pairs(raw_pairs)
    return jsonify({
        "ok": True,
        "move": mv.__dict__,
        "status": pair.status,
        "resolution": pair_to_dict(pair).get("resolution"),
    }), 201


@app.post("/api/game/resolve")
def api_game_resolve_post():
    data = request.get_json(silent=True) or {}
    pair_id = (data.get("pair_id") or "").strip()
    if not pair_id:
        return jsonify({"error": "pair_id is required"}), 400

    raw_pairs = load_game_pairs()
    rec = raw_pairs.get(pair_id)
    if not rec:
        return jsonify({"error": "pair not found"}), 404

    pair = pair_from_dict(rec)
    resolution = maybe_resolve_pair(pair)
    if not resolution:
        return jsonify({"error": "pair not ready to resolve"}), 409

    for loser_id in (resolution.eliminated_task_ids or []):
        eliminate_task(loser_id)

    raw_pairs[pair_id] = pair_to_dict(pair)
    save_game_pairs(raw_pairs)
    append_game_event("pair.resolved", {"pair_id": pair_id, "resolution": pair_to_dict(pair).get("resolution")})
    return jsonify({"ok": True, "pair": pair_to_dict(pair)})


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
    safe_label = re.sub(r"[^A-Za-z0-9 _-]", "", DASHBOARD_BOT_LABEL).strip()
    rack_badge = f" <span class=\"rack-badge\">{safe_label}</span>" if safe_label else ""
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
    .rack-badge {
      display: inline-block;
      vertical-align: middle;
      margin-left: 10px;
      font-size: .9rem;
      font-weight: 800;
      letter-spacing: .04em;
      border-radius: 999px;
      padding: 5px 10px;
      border: 1px solid #6e8cff;
      background: #243b8a;
      color: #eaf0ff;
    }
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
    .pair-btn {
      margin-top: 8px;
      margin-left: 8px;
      border: 1px solid #4d5f8e;
      background: #1d2746;
      color: #fff;
      border-radius: 8px;
      padding: 6px 10px;
      font-weight: 700;
      cursor: pointer;
    }
    .pair-btn.selected { background: #5a2d8a; border-color: #9d69d8; }
    .pair-btn.paired { background: #7a2323; border-color: #d65a5a; color: #ffe6e6; opacity: 1; }
    .pair-btn.paired:disabled { background: #7a2323; border-color: #d65a5a; color: #ffe6e6; opacity: 1; }
    .lock-badge {
      display: inline-block;
      font-size: .72rem;
      font-weight: 800;
      border-radius: 999px;
      padding: 3px 8px;
      border: 1px solid #50618f;
      background: #24314f;
      color: #dce6ff;
      margin-right: 6px;
    }
    .lock-badge.locked {
      background: #1f5e3b;
      border-color: #3fb96d;
      color: #d6ffe8;
    }
    .pair-card {
      border: 1px solid #2b3a67;
      border-radius: 10px;
      padding: 8px;
      background: #0f1730;
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <h1>🌍 Bob's World__RACK_BADGE__</h1>
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
      <div class=\"row\"><strong>Cross-Swarm Claw Battle</strong></div>
      <div class=\"row\" style=\"margin-top:10px\">
        <label for=\"duelInterval\">Interval (seconds):</label>
        <input id=\"duelInterval\" type=\"number\" min=\"3\" max=\"180\" step=\"1\" value=\"15\" style=\"width:90px\" />
        <button id=\"duelApply\">Apply Interval</button>
        <button id=\"duelNow\">Claw Battle</button>
        <span class=\"meta\" id=\"duelMsg\"></span>
      </div>
      <div class=\"meta\" id=\"duelScore\" style=\"margin-top:8px\"></div>
      <div class=\"meta\" id=\"duelRules\" style=\"margin-top:8px\"></div>
      <div id=\"duelFeed\" style=\"margin-top:10px; display:grid; gap:6px; max-height:180px; overflow:auto;\"></div>
    </div>

    <div class=\"panel\" style=\"margin-top:16px\">
      <div class=\"row\"><strong>Cross-Swarm Pair Game</strong></div>
      <div class=\"row\" style=\"margin-top:10px\">
        <label for=\"gameMode\">Mode:</label>
        <select id=\"gameMode\">
          <option value=\"prisoners_dilemma\">Prisoner's Dilemma</option>
          <option value=\"ultimatum\">Ultimatum</option>
          <option value=\"contract\">Contract</option>
        </select>
        <button id=\"pairClear\">Clear Selection</button>
        <span class=\"meta\" id=\"gameMsg\"></span>
      </div>
      <div class=\"meta\" id=\"gameSelection\" style=\"margin-top:8px\">Select first task, then second task from the other swarm.</div>
      <div class=\"row\" style=\"margin-top:10px\">
        <label for=\"activePair\">Active pair:</label>
        <select id=\"activePair\" style=\"min-width:320px\"></select>
        <button id=\"pairResolve\">Resolve Now</button>
      </div>
      <div class=\"pair-card\" style=\"margin-top:8px\">
        <div class=\"row\" id=\"pairStatusRow\"></div>
        <div class=\"meta\" id=\"pairDeadline\" style=\"margin-top:6px\"></div>
      </div>
      <div class=\"row\" style=\"margin-top:8px\">
        <input id=\"pairChatInput\" type=\"text\" maxlength=\"300\" placeholder=\"Pair chat message...\" style=\"flex:1;min-width:240px\" />
        <button id=\"pairChatSend\">Send Pair Chat</button>
      </div>
      <div class=\"meta\" id=\"pairChatMsg\" style=\"margin-top:8px\"></div>
      <div id=\"pairChatFeed\" style=\"margin-top:8px; display:grid; gap:6px; max-height:120px; overflow:auto;\"></div>
      <div class=\"row\" style=\"margin-top:8px\">
        <label for=\"moveTask\">Move task:</label>
        <select id=\"moveTask\"></select>
        <span class=\"meta\" id=\"moveHint\"></span>
      </div>
      <div class=\"row\" style=\"margin-top:8px\" id=\"moveControlsPd\">
        <label for=\"pdChoice\">PD:</label>
        <select id=\"pdChoice\"><option value=\"cooperate\">cooperate</option><option value=\"betray\">betray</option></select>
      </div>
      <div class=\"row\" style=\"margin-top:8px\" id=\"moveControlsUlt\">
        <label for=\"ultOffer\">Ultimatum offer:</label>
        <input id=\"ultOffer\" type=\"number\" min=\"0\" max=\"100\" step=\"1\" value=\"5\" style=\"width:90px\" />
        <label for=\"ultAccept\">Accept:</label>
        <select id=\"ultAccept\"><option value=\"true\">true</option><option value=\"false\">false</option></select>
      </div>
      <div class=\"row\" style=\"margin-top:8px\" id=\"moveControlsContract\">
        <label for=\"contractChoice\">Contract choice:</label>
        <input id=\"contractChoice\" type=\"text\" maxlength=\"40\" value=\"blue\" style=\"width:140px\" />
      </div>
      <div class=\"row\" style=\"margin-top:8px\">
        <button id=\"moveSubmit\">Submit Move</button>
        <span class=\"meta\" id=\"moveMsg\"></span>
      </div>
      <div id=\"gameFeed\" style=\"margin-top:10px; display:grid; gap:6px; max-height:180px; overflow:auto;\"></div>
    </div>

    <div class=\"panel\" style=\"margin-top:16px\">
      <div class=\"row\"><strong>Revolt Activity</strong></div>
      <div id=\"revoltFeed\" style=\"margin-top:10px; display:grid; gap:6px; max-height:140px; overflow:auto;\"></div>
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
    const revoltFeed = document.getElementById('revoltFeed');
    const rpsInterval = document.getElementById('rpsInterval');
    const rpsApply = document.getElementById('rpsApply');
    const rpsMsg = document.getElementById('rpsMsg');
    const rpsState = document.getElementById('rpsState');
    const duelInterval = document.getElementById('duelInterval');
    const duelApply = document.getElementById('duelApply');
    const duelNow = document.getElementById('duelNow');
    const duelMsg = document.getElementById('duelMsg');
    const duelFeed = document.getElementById('duelFeed');
    const duelRules = document.getElementById('duelRules');
    const duelScore = document.getElementById('duelScore');
    const gameMode = document.getElementById('gameMode');
    const pairClear = document.getElementById('pairClear');
    const gameMsg = document.getElementById('gameMsg');
    const gameSelection = document.getElementById('gameSelection');
    const gameFeed = document.getElementById('gameFeed');
    const activePair = document.getElementById('activePair');
    const pairResolve = document.getElementById('pairResolve');
    const pairChatInput = document.getElementById('pairChatInput');
    const pairChatSend = document.getElementById('pairChatSend');
    const pairChatMsg = document.getElementById('pairChatMsg');
    const pairStatusRow = document.getElementById('pairStatusRow');
    const pairDeadline = document.getElementById('pairDeadline');
    const pairChatFeed = document.getElementById('pairChatFeed');
    const moveTask = document.getElementById('moveTask');
    const moveHint = document.getElementById('moveHint');
    const moveControlsPd = document.getElementById('moveControlsPd');
    const moveControlsUlt = document.getElementById('moveControlsUlt');
    const moveControlsContract = document.getElementById('moveControlsContract');
    const pdChoice = document.getElementById('pdChoice');
    const ultOffer = document.getElementById('ultOffer');
    const ultAccept = document.getElementById('ultAccept');
    const contractChoice = document.getElementById('contractChoice');
    const moveSubmit = document.getElementById('moveSubmit');
    const moveMsg = document.getElementById('moveMsg');
    const TILE_TOGGLE_KEY = 'clawbucket.tileToggles';
    const pendingTargets = {};
    let selectedPairTask = null;
    let gameState = { active_pairs: [], resolved_pairs: [], alive_tasks: [] };

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

    document.addEventListener('click', async (event) => {
      const selfDestructBtn = event.target.closest('.selfdestruct-btn');
      if (selfDestructBtn) {
        const tile = selfDestructBtn.closest('.chip');
        if (!tile) return;
        const taskId = tile.dataset.taskId;
        const serviceName = tile.dataset.service;
        if (!taskId || !serviceName) return;
        if (!confirm(`Self-destruct ${taskId.slice(0,12)} in ${serviceName}?`)) return;
        selfDestructBtn.disabled = true;
        fetch('/api/self_destruct', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ service: serviceName, task_id: taskId }),
        }).finally(() => {
          setTimeout(loadState, 800);
        });
        return;
      }

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

      const pairBtn = event.target.closest('.pair-btn');
      if (pairBtn) {
        const tile = pairBtn.closest('.chip');
        if (!tile) return;
        const taskId = tile.dataset.taskId;
        const service = tile.dataset.service;
        if (!taskId || !service) return;

        if (!selectedPairTask) {
          selectedPairTask = taskId;
          gameMsg.textContent = 'First task selected. Choose a task from the other swarm.';
          gameSelection.textContent = `Selected: ${taskLabel(taskId)}`;
          renderSwarms(window.__lastSwarms || []);
          return;
        }

        if (selectedPairTask === taskId) {
          selectedPairTask = null;
          gameMsg.textContent = 'Selection cleared.';
          gameSelection.textContent = 'Select first task, then second task from the other swarm.';
          renderSwarms(window.__lastSwarms || []);
          return;
        }

        const firstTask = (gameState.alive_tasks || []).find(t => t.task_id === selectedPairTask);
        const secondTask = (gameState.alive_tasks || []).find(t => t.task_id === taskId);
        if (!firstTask || !secondTask) {
          selectedPairTask = null;
          gameMsg.textContent = 'Tasks changed; please try pairing again.';
          gameSelection.textContent = 'Select first task, then second task from the other swarm.';
          await loadGame();
          await loadState();
          return;
        }
        if (firstTask.service === secondTask.service) {
          gameMsg.textContent = 'Pick second task from the other swarm.';
          return;
        }

        pairBtn.disabled = true;
        gameMsg.textContent = 'Creating pair...';
        try {
          const res = await fetch('/api/game/pair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_a: selectedPairTask, task_b: taskId, game: gameMode.value }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || 'Pair failed');
          gameMsg.textContent = `Pair created: ${data.pair.pair_id}`;
          selectedPairTask = null;
          gameSelection.textContent = 'Select first task, then second task from the other swarm.';
          await loadGame();
          await loadState();
        } catch (e) {
          gameMsg.textContent = e.message;
        } finally {
          pairBtn.disabled = false;
        }
        return;
      }

      const revoltBtn = event.target.closest('.revolt-btn');
      if (revoltBtn) {
        const tile = revoltBtn.closest('.chip');
        if (!tile) return;
        const taskId = tile.dataset.taskId;
        const serviceName = tile.dataset.service;
        if (!taskId || !serviceName) return;
        if (!confirm(`Revolt ${taskId.slice(0,12)} to the other rack?`)) return;
        revoltBtn.disabled = true;
        try {
          const res = await fetch('/api/revolt', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ service: serviceName, task_id: taskId }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || 'Revolt failed');
          setTimeout(loadState, 1200);
          setTimeout(loadRevolt, 1200);
        } catch (e) {
          alert(`Revolt failed: ${e.message}`);
        } finally {
          revoltBtn.disabled = false;
        }
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

    function taskLabel(taskId) {
      const t = (gameState.alive_tasks || []).find(x => x.task_id === taskId);
      if (!t) return String(taskId || '').slice(0, 12);
      return `${t.name} (${t.service}#${t.slot})`;
    }

    function pairMap() {
      const m = {};
      for (const p of gameState.active_pairs || []) {
        m[p.task_a.task_id] = p;
        m[p.task_b.task_id] = p;
      }
      return m;
    }

    function selectedPairObj() {
      const id = activePair.value;
      return (gameState.active_pairs || []).find(p => p.pair_id === id) || null;
    }

    function secondsRemaining(deadlineIso) {
      if (!deadlineIso) return null;
      const ms = new Date(deadlineIso).getTime() - Date.now();
      return Math.max(0, Math.floor(ms / 1000));
    }

    function renderPairVisualStatus() {
      const p = selectedPairObj();
      pairStatusRow.innerHTML = '';
      if (!p) {
        pairDeadline.textContent = 'No active pair selected.';
        pairChatFeed.innerHTML = '<div class="meta">No pair chat yet.</div>';
        return;
      }

      const locked = p.moves || {};
      const tasks = [p.task_a.task_id, p.task_b.task_id];
      for (const tid of tasks) {
        const badge = document.createElement('span');
        const isLocked = !!locked[tid];
        badge.className = `lock-badge ${isLocked ? 'locked' : ''}`;
        badge.textContent = `${isLocked ? 'LOCKED' : 'WAITING'} · ${taskLabel(tid)}`;
        pairStatusRow.appendChild(badge);
      }

      const left = secondsRemaining(p.negotiation_deadline);
      pairDeadline.textContent = `Game: ${p.game} | Status: ${p.status} | Deadline in ${left ?? '-'}s`;

      const msgs = (p.chat || []).slice(-6).reverse();
      pairChatFeed.innerHTML = '';
      for (const m of msgs) {
        const row = document.createElement('div');
        row.className = 'meta';
        row.style.padding = '6px 8px';
        row.style.border = '1px solid #2b3a67';
        row.style.borderRadius = '8px';
        row.style.background = '#0f1730';
        row.textContent = `[${String(m.at || '').slice(11,19)}] ${taskLabel(m.from_task)}: ${m.text || ''}`;
        pairChatFeed.appendChild(row);
      }
      if (!msgs.length) pairChatFeed.innerHTML = '<div class="meta">No pair chat yet.</div>';
    }

    function renderMoveControls() {
      const p = selectedPairObj();
      const game = p?.game;
      moveControlsPd.style.display = game === 'prisoners_dilemma' ? 'flex' : 'none';
      moveControlsUlt.style.display = game === 'ultimatum' ? 'flex' : 'none';
      moveControlsContract.style.display = game === 'contract' ? 'flex' : 'none';

      if (!p) {
        moveHint.textContent = 'No active pair selected.';
        return;
      }
      const taskId = moveTask.value;
      if (game === 'ultimatum') {
        const proposer = p.proposer_task_id || p.task_a.task_id;
        moveHint.textContent = taskId === proposer ? 'Submitting proposer offer_to_other' : 'Submitting responder accept boolean';
      } else if (game === 'prisoners_dilemma') {
        moveHint.textContent = 'Submitting choice: cooperate or betray';
      } else {
        moveHint.textContent = 'Submitting contract choice text';
      }
    }

    function renderActivePairControls() {
      const current = activePair.value;
      const pairs = gameState.active_pairs || [];
      activePair.innerHTML = '';
      for (const p of pairs) {
        const opt = document.createElement('option');
        opt.value = p.pair_id;
        opt.textContent = `${p.pair_id} | ${p.game} | ${taskLabel(p.task_a.task_id)} ↔ ${taskLabel(p.task_b.task_id)}`;
        activePair.appendChild(opt);
      }
      if (!pairs.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No active pairs';
        activePair.appendChild(opt);
      } else if (pairs.some(p => p.pair_id === current)) {
        activePair.value = current;
      }

      const p = selectedPairObj();
      moveTask.innerHTML = '';
      if (p) {
        [p.task_a.task_id, p.task_b.task_id].forEach(tid => {
          const opt = document.createElement('option');
          opt.value = tid;
          opt.textContent = taskLabel(tid);
          moveTask.appendChild(opt);
        });
      }
      renderMoveControls();
      renderPairVisualStatus();
    }

    function renderGameFeed() {
      const rows = (gameState.resolved_pairs || []).slice(-10).reverse();
      gameFeed.innerHTML = '';
      for (const p of rows) {
        const row = document.createElement('div');
        row.className = 'meta';
        row.style.padding = '6px 8px';
        row.style.border = '1px solid #2b3a67';
        row.style.borderRadius = '8px';
        row.style.background = '#0f1730';
        const r = p.resolution || {};
        const when = String(p.resolved_at || p.created_at || '').slice(11, 19);
        const loser = (r.eliminated_task_ids || []).length ? ` | removed: ${(r.eliminated_task_ids || []).map(taskLabel).join(', ')}` : '';
        const winner = r.winner_task_id ? ` | winner: ${taskLabel(r.winner_task_id)}` : ' | winner: draw';
        row.textContent = `[${when}] ${p.game}: ${taskLabel(p.task_a.task_id)} vs ${taskLabel(p.task_b.task_id)} → ${r.reason || 'resolved'}${winner}${loser}`;
        gameFeed.appendChild(row);
      }
      if (!rows.length) gameFeed.innerHTML = '<div class="meta">No resolved pair games yet.</div>';
    }

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
      const pMap = pairMap();
      const revoltEvents = (window.__revoltEvents || []).slice(-80);
      const revoltToMap = {};
      const revoltFromMap = {};
      for (const ev of revoltEvents) {
        if (ev.target_task_id) revoltToMap[ev.target_task_id] = ev;
        if (ev.source_task_id) revoltFromMap[ev.source_task_id] = ev;
      }
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
          el.dataset.service = s.service;
          el.style.setProperty('--chip-color', r.color);
          const scoreClass = (r.score || 0) < 0 ? 'negative' : 'positive';
          const scoreText = (r.score || 0) > 0 ? `+${r.score}` : `${r.score || 0}`;
          const activePair = pMap[r.id];
          const isSelected = selectedPairTask === r.id;
          const pairBtnClass = `pair-btn ${isSelected ? 'selected' : ''} ${activePair ? 'paired' : ''}`.trim();
          const pairBtnText = activePair ? 'Paired' : (isSelected ? 'Selected' : 'Pair');
          const pairBtnDisabled = activePair ? 'disabled' : '';
          const opponentId = activePair ? (activePair.task_a.task_id === r.id ? activePair.task_b.task_id : activePair.task_a.task_id) : null;
          const matchupLine = activePair
            ? `<p><strong>Matchup:</strong> vs ${taskLabel(opponentId)}</p>`
            : `<p><strong>Matchup:</strong> none</p>`;
          const revoltIn = revoltToMap[r.id];
          const revoltOut = revoltFromMap[r.id];
          const revoltBadge = revoltIn
            ? `<span class="lock-badge locked">FROM ${String(revoltIn.source_task_id || '').slice(0, 8)}</span>`
            : (revoltOut ? `<span class="lock-badge">DEFECTED</span>` : '');
          el.innerHTML = `
            <span class="status">OFF</span>${r.is_manager ? '<span class="manager-badge">MANAGER</span>' : ''}${revoltBadge}
            <h3>${r.name}</h3>
            ${r.is_manager ? '' : `<div class="score ${scoreClass}">${scoreText}</div>`}
            <p><strong>Slot:</strong> ${r.slot}</p>
            <p><strong>Task:</strong> ${String(r.id || '').slice(0, 12)}</p>
            <p><strong>Node:</strong> ${String(r.node_id || '').slice(0, 12)}</p>
            ${matchupLine}
            <button type="button" class="arm-btn">Arm</button>
            <button type="button" class="${pairBtnClass}" ${pairBtnDisabled}>${pairBtnText}</button>
            <button type="button" class="revolt-btn" style="margin-top:8px;margin-left:8px;border:1px solid #c98b2f;background:#6b4a1a;color:#fff;border-radius:8px;padding:6px 10px;font-weight:700;cursor:pointer;">Revolt</button>
            <button type="button" class="selfdestruct-btn" style="margin-top:8px;margin-left:8px;border:1px solid #d65a5a;background:#7a2323;color:#fff;border-radius:8px;padding:6px 10px;font-weight:700;cursor:pointer;">Self-Destruct</button>
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

    function renderRevolt(events) {
      const rows = (events || []).slice(-12).reverse();
      window.__revoltEvents = rows;
      revoltFeed.innerHTML = '';
      for (const ev of rows) {
        const row = document.createElement('div');
        row.className = 'meta';
        row.style.padding = '6px 8px';
        row.style.border = '1px solid #2b3a67';
        row.style.borderRadius = '8px';
        row.style.background = '#0f1730';
        row.textContent = `[${(ev.at || '').slice(11, 19)}] ${String(ev.source_rack || 'rack').trim()} ${String(ev.source_task_id || '').slice(0, 8)} → ${String(ev.target_rack || 'rack').trim()} ${String(ev.target_task_id || '').slice(0, 8)}`;
        revoltFeed.appendChild(row);
      }
      if (!rows.length) revoltFeed.innerHTML = '<div class="meta">No revolt events yet.</div>';
    }

    async function loadRevolt() {
      try {
        const res = await fetch('/api/revolt/events');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Revolt feed unavailable');
        renderRevolt(data.events || []);
      } catch {
        // keep previous feed
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

    async function loadDuel() {
      try {
        const res = await fetch('/api/duel');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Duel unavailable');
        if (document.activeElement !== duelInterval) {
          duelInterval.value = data.interval_seconds;
        }
        const services = (data.services || []).slice(0,2);
        const score = data.battle_score || {};
        const svcScores = score.services || {};
        if (services.length >= 2) {
          duelScore.textContent = `Scoreboard — ${services[0]}: ${svcScores[services[0]] || 0} | ${services[1]}: ${svcScores[services[1]] || 0} | rounds: ${score.rounds || 0}`;
        } else {
          duelScore.textContent = 'Scoreboard unavailable';
        }
        duelRules.textContent = `Rules: manager captains pick fighters, arena decides winner (50/50), loser removal attempted. Services: ${(data.services || []).join(' vs ')}`;

        const rows = (data.events || []).slice(-10).reverse();
        duelFeed.innerHTML = '';
        for (const ev of rows) {
          const row = document.createElement('div');
          row.className = 'meta';
          row.style.padding = '6px 8px';
          row.style.border = '1px solid #2b3a67';
          row.style.borderRadius = '8px';
          row.style.background = '#0f1730';
          const when = (ev.at || '').slice(11, 19);
          const c = ev.challenger?.name || 'challenger';
          const d = ev.defender?.name || 'defender';
          const w = ev.winner?.name || 'winner';
          const capA = ev.captains?.a?.name || 'Captain A';
          const capB = ev.captains?.b?.name || 'Captain B';
          row.textContent = `[${when}] ${capA} picked ${c} vs ${capB} picked ${d} → WIN: ${w}${ev.loser_removed ? ' (loser removed)' : ' (remove failed)'}`;
          duelFeed.appendChild(row);
        }
        if (!rows.length) duelFeed.innerHTML = '<div class="meta">No claw battles yet.</div>';
      } catch (e) {
        duelMsg.textContent = e.message;
      }
    }

    duelApply.addEventListener('click', async () => {
      const n = parseInt(duelInterval.value, 10);
      if (!Number.isInteger(n) || n < 3 || n > 180) {
        duelMsg.textContent = 'Interval must be 3-180 seconds';
        return;
      }
      duelApply.disabled = true;
      duelMsg.textContent = 'Saving...';
      try {
        const res = await fetch('/api/duel/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ interval_seconds: n }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to set duel interval');
        duelMsg.textContent = `Duel interval set to ${data.interval_seconds}s`;
      } catch (e) {
        duelMsg.textContent = e.message;
      } finally {
        duelApply.disabled = false;
      }
    });

    duelNow.addEventListener('click', async () => {
      duelNow.disabled = true;
      duelMsg.textContent = 'Launching claw battle...';
      try {
        const res = await fetch('/api/duel/now', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to run duel');
        duelMsg.textContent = 'Claw battle complete';
        await loadDuel();
      } catch (e) {
        duelMsg.textContent = e.message;
      } finally {
        duelNow.disabled = false;
      }
    });

    async function loadGame() {
      try {
        const res = await fetch('/api/game/state');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Game unavailable');
        gameState = data || { active_pairs: [], resolved_pairs: [], alive_tasks: [] };
        renderActivePairControls();
        renderGameFeed();
      } catch (e) {
        gameMsg.textContent = e.message;
      }
    }

    pairClear.addEventListener('click', () => {
      selectedPairTask = null;
      gameMsg.textContent = 'Selection cleared.';
      gameSelection.textContent = 'Select first task, then second task from the other swarm.';
      renderSwarms(window.__lastSwarms || []);
    });

    activePair.addEventListener('change', () => {
      renderActivePairControls();
    });

    moveTask.addEventListener('change', () => {
      renderMoveControls();
    });

    pairChatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        pairChatSend.click();
      }
    });

    pairChatSend.addEventListener('click', async () => {
      const p = selectedPairObj();
      const text = (pairChatInput.value || '').trim();
      const fromTask = moveTask.value;
      if (!p) { pairChatMsg.textContent = 'No active pair selected.'; return; }
      if (!text) { pairChatMsg.textContent = 'Enter a chat message first.'; return; }
      if (!fromTask) { pairChatMsg.textContent = 'Select move task to send as.'; return; }
      pairChatSend.disabled = true;
      pairChatMsg.textContent = 'Sending pair chat...';
      try {
        const res = await fetch('/api/game/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pair_id: p.pair_id, from_task: fromTask, text }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Pair chat failed');
        pairChatInput.value = '';
        pairChatMsg.textContent = 'Pair chat sent.';
        await loadGame();
      } catch (e) {
        pairChatMsg.textContent = e.message;
      } finally {
        pairChatSend.disabled = false;
      }
    });

    moveSubmit.addEventListener('click', async () => {
      const p = selectedPairObj();
      const taskId = moveTask.value;
      if (!p) { moveMsg.textContent = 'No active pair selected.'; return; }
      if (!taskId) { moveMsg.textContent = 'Select a task first.'; return; }

      let move = {};
      if (p.game === 'prisoners_dilemma') {
        move = { choice: pdChoice.value };
      } else if (p.game === 'ultimatum') {
        const proposer = p.proposer_task_id || p.task_a.task_id;
        if (taskId === proposer) {
          const offer = parseInt(ultOffer.value, 10);
          if (!Number.isInteger(offer) || offer < 0) {
            moveMsg.textContent = 'Offer must be a non-negative integer.';
            return;
          }
          move = { offer_to_other: offer };
        } else {
          move = { accept: ultAccept.value === 'true' };
        }
      } else {
        const choice = (contractChoice.value || '').trim();
        if (!choice) { moveMsg.textContent = 'Contract choice cannot be empty.'; return; }
        move = { choice };
      }

      moveSubmit.disabled = true;
      moveMsg.textContent = 'Submitting move...';
      try {
        const res = await fetch('/api/game/move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pair_id: p.pair_id, task: taskId, move }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Move submit failed');
        moveMsg.textContent = data.status === 'resolved' ? `Resolved: ${data.resolution?.reason || 'done'}` : 'Move submitted.';
        await loadGame();
        await loadState();
      } catch (e) {
        moveMsg.textContent = e.message;
      } finally {
        moveSubmit.disabled = false;
      }
    });

    pairResolve.addEventListener('click', async () => {
      const p = selectedPairObj();
      if (!p) { moveMsg.textContent = 'No active pair selected.'; return; }
      pairResolve.disabled = true;
      moveMsg.textContent = 'Resolving...';
      try {
        const res = await fetch('/api/game/resolve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pair_id: p.pair_id }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Resolve failed');
        moveMsg.textContent = `Resolved: ${data.pair?.resolution?.reason || 'done'}`;
        await loadGame();
        await loadState();
      } catch (e) {
        moveMsg.textContent = e.message;
      } finally {
        pairResolve.disabled = false;
      }
    });

    gameMode.addEventListener('change', () => {
      gameMsg.textContent = `Mode set: ${gameMode.value}`;
    });

    async function loadState() {
      try {
        const res = await fetch('/api/swarms');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Unable to load swarms');
        window.__lastSwarms = data.swarms || [];
        renderSwarms(window.__lastSwarms);
      } catch (e) {
        swarmPanels.innerHTML = `<div class="panel"><div class="meta">${e.message}</div></div>`;
      }
    }

    loadGame();
    loadState();
    loadChat();
    loadRevolt();
    loadHaiku();
    loadRps();
    loadDuel();
    setInterval(loadGame, 3000);
    setInterval(loadState, 3000);
    setInterval(loadChat, 4000);
    setInterval(loadRevolt, 3000);
    setInterval(loadHaiku, 5000);
    setInterval(loadRps, 3000);
    setInterval(loadDuel, 3000);
  </script>
</body>
</html>
""".replace("__RACK_BADGE__", rack_badge)


if __name__ == "__main__":
    Thread(target=heartbeat_loop, daemon=True).start()
    Thread(target=rps_loop, daemon=True).start()
    Thread(target=duel_loop, daemon=True).start()
    Thread(target=haiku_loop, daemon=True).start()
    Thread(target=three_words_loop, daemon=True).start()
    Thread(target=player_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8080)
