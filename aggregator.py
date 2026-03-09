from flask import Flask, jsonify
import json
import os
from collections import defaultdict

from pymemcache.client.base import Client as MemcacheClient

app = Flask(__name__)

MEMCACHED_HOST = os.environ.get("MEMCACHED_HOST", "memcached")
MEMCACHED_PORT = int(os.environ.get("MEMCACHED_PORT", "11211"))
ARM_EVENTS_KEY = "clawbucket:arm:events"


def memcache_client():
    return MemcacheClient((MEMCACHED_HOST, MEMCACHED_PORT), connect_timeout=0.5, timeout=0.5)


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
            return data
    except Exception:
        pass
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
    return []


def score_from_events(events):
    by_bot = defaultdict(lambda: {"bot": "", "task_id": "", "on": 0, "off": 0, "toggles": 0, "score": 0})
    for ev in events:
        bot = ev.get("bot") or "unknown"
        task_id = ev.get("task_id") or ""
        state = (ev.get("state") or "").lower()

        row = by_bot[task_id or bot]
        row["bot"] = bot
        row["task_id"] = task_id
        row["toggles"] += 1
        if state == "on":
            row["on"] += 1
            row["score"] += 1
        elif state == "off":
            row["off"] += 1
            row["score"] -= 1

    scoreboard = sorted(by_bot.values(), key=lambda r: (r["score"], r["toggles"]), reverse=True)
    return scoreboard


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/scoreboard")
def api_scoreboard():
    events = load_arm_events()
    board = score_from_events(events)
    return jsonify({
        "events": len(events),
        "scoreboard": board,
        "scoring": "score = +1 for ARM ON, -1 for ARM OFF",
        "source": "memcached",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
