from flask import Flask, jsonify, request
import hashlib
import os
import socket
from datetime import datetime, timezone
from threading import Thread

import docker
from docker.errors import DockerException, NotFound

app = Flask(__name__)

STARTED_AT = datetime.now(timezone.utc).isoformat()
HOSTNAME = socket.gethostname()
SERVICE_NAME = os.environ.get("SWARM_SERVICE", "clawbucket_clawbucket")


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
                }
            )

    running.sort(key=lambda x: x["slot"])
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
    .status { display: inline-block; font-size: .78rem; font-weight: 700; letter-spacing: .02em; padding: 4px 8px; border-radius: 999px; background: #1b2f66; }
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
  </div>

  <script>
    const grid = document.getElementById('grid');
    const svc = document.getElementById('svc');
    const counts = document.getElementById('counts');
    const replicasInput = document.getElementById('replicas');
    const applyBtn = document.getElementById('apply');
    const msg = document.getElementById('msg');
    let pendingTarget = null;
    let currentDesired = null;
    let currentRunning = null;


    async function loadState() {
      try {
        const res = await fetch('/api/swarm');
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Unable to load swarm data');

        svc.textContent = `service: ${data.service}`;
        counts.textContent = `running: ${data.running_count} / desired: ${data.desired_replicas}`;
        currentDesired = data.desired_replicas;
        currentRunning = data.running_count;

        if (pendingTarget === null) {
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

        grid.innerHTML = '';
        for (const r of data.replicas) {
          const el = document.createElement('div');
          el.className = 'chip';
          el.style.setProperty('--chip-color', r.color);
          el.innerHTML = `
            <span class="status">${r.state.toUpperCase()}</span>
            <h3>${r.name}</h3>
            <p><strong>Slot:</strong> ${r.slot}</p>
            <p><strong>Task:</strong> ${r.id.slice(0, 12)}</p>
            <p><strong>Node:</strong> ${r.node_id.slice(0, 12)}</p>
          `;
          grid.appendChild(el);
        }
      } catch (e) {
        msg.textContent = e.message;
      }
    }

    applyBtn.addEventListener('click', async () => {
      const replicas = parseInt(replicasInput.value, 10);

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
    setInterval(loadState, 3000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
