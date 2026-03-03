from flask import Flask, jsonify
import hashlib
import os
import socket
from datetime import datetime, timezone

app = Flask(__name__)

STARTED_AT = datetime.now(timezone.utc).isoformat()
HOSTNAME = socket.gethostname()


def color_from_text(text: str) -> str:
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    return f"#{digest[:6]}"


@app.get("/api/whoami")
def whoami():
    return jsonify(
        {
            "hostname": HOSTNAME,
            "started_at_utc": STARTED_AT,
            "service": os.environ.get("SERVICE_NAME", "clawbucket"),
            "swarm_node": os.environ.get("SWARM_NODE", "unknown"),
            "task_name": os.environ.get("TASK_NAME", "unknown"),
            "task_slot": os.environ.get("TASK_SLOT", "unknown"),
            "task_id": os.environ.get("TASK_ID", "unknown"),
            "color": color_from_text(HOSTNAME),
        }
    )


@app.get("/")
def index():
    data = whoami().json
    color = data["color"]
    return f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>clawbucket swarm visual QA</title>
  <style>
    :root {{ font-family: Inter, system-ui, Arial, sans-serif; }}
    body {{ margin: 0; background: #0f172a; color: #e2e8f0; }}
    .wrap {{ max-width: 720px; margin: 32px auto; padding: 0 16px; }}
    .card {{ background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 20px; }}
    .pill {{ display: inline-block; padding: 6px 10px; border-radius: 999px; font-weight: 700; background: {color}; color: white; }}
    h1 {{ margin-top: 0; font-size: 1.4rem; }}
    dl {{ display: grid; grid-template-columns: 170px 1fr; gap: 8px 12px; margin: 16px 0; }}
    dt {{ color: #94a3b8; }}
    dd {{ margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-word; }}
    .hint {{ color: #93c5fd; font-size: 0.95rem; margin-top: 14px; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <h1>clawbucket • Swarm visual QA</h1>
      <span class=\"pill\">instance color {color}</span>
      <dl>
        <dt>hostname</dt><dd>{data['hostname']}</dd>
        <dt>service</dt><dd>{data['service']}</dd>
        <dt>swarm node</dt><dd>{data['swarm_node']}</dd>
        <dt>task name</dt><dd>{data['task_name']}</dd>
        <dt>task slot</dt><dd>{data['task_slot']}</dd>
        <dt>task id</dt><dd>{data['task_id']}</dd>
        <dt>started at (UTC)</dt><dd>{data['started_at_utc']}</dd>
      </dl>
      <div class=\"hint\">Refresh repeatedly. In a replicated Swarm service, hostname/task details and color should rotate as requests hit different replicas.</div>
    </div>
  </div>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
