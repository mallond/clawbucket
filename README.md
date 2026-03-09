# clawbucket

**Bob's World** is now a Docker Swarm simulation game platform:

- Swarm replica tiles with per-tile ARM state
- Manager/leader visualization
- Memcached-backed inter-container signaling
- Leader-published Rock/Paper/Scissors (RPS) rounds
- Non-leader player scoring with live tile display
- Aggregator API for scoreboard + current ON/OFF state
- Ollama service for local model pulls/tests (`smollm2:135m`)

This README captures the current behavior in detail so we can evolve it safely and eventually extract a formal `SKILL.md`.

---

## 1) What this system does

### Core concept
A Swarm service (`clawbucket`) runs multiple replicas. The UI shows one tile per running task. Containers coordinate through Memcached to simulate lightweight distributed game behavior.

### Current features

1. **Swarm dashboard (port 8080)**
   - Shows running replicas (`slot`, short `task id`, short `node id`, generated name)
   - Scale control (`/api/scale`) with desired/running status

2. **ARM toggles per tile**
   - Each tile has an `Arm` button
   - OFF = default style
   - ON = green button + green ON status pill
   - Every arm toggle emits an event to Memcached (`on` / `off`)

3. **Single manager tile highlight**
   - Exactly one tile is marked `MANAGER`
   - Selection follows Swarm leader-aware rule with deterministic fallback

4. **Container conversation (simulation)**
   - UI panel posts/reads chat messages from Memcached

5. **Task heartbeats**
   - Each task writes periodic liveness text to Memcached:
     - `Ping from <task name> at <timestamp>`
   - Interval: every 10s
   - TTL: 20s

6. **RPS leader broadcast + players**
   - Exactly one elected publisher task writes R/P/S choice to Memcached
   - Interval configurable from UI
   - Non-manager tasks act as players:
     - roll random rock/paper/scissors each round
     - compare against latest leader move
     - update per-task score (+1 win, -1 loss, 0 tie)

7. **Aggregator service (port 8090)**
   - Reads ARM events from Memcached
   - Exposes scoreboard with:
     - counts (`on`, `off`, `toggles`)
     - cumulative `score`
     - `current_on_state`
     - `last_state`, `last_event_at`

8. **Ollama model service (port 11434)**
   - Dedicated service for local LLM runtime in Swarm
   - Persistent model storage via named volume
   - Verified model: `smollm2:135m`

---

## 2) Architecture

### Services

Defined in `docker-stack.yml`:

- `clawbucket`
  - Flask app (`app.py`), UI + APIs
  - Exposes `8080`
  - Mounts Docker socket for Swarm state/scale APIs

- `clawbucket-aggregator`
  - Flask app (`aggregator.py`)
  - Exposes `8090`

- `memcached`
  - Shared transient state bus
  - Internal-only (no host port)

- `ollama`
  - Local inference/model service
  - Exposes `11434`
  - Uses volume `ollama_data` for model persistence

### Data flow

1. User interacts with 8080 UI
2. App writes/reads operational state from Swarm + Memcached
3. Leader publisher emits shared RPS state
4. Non-leader tasks consume RPS state and update scores
5. Aggregator reads Memcached event streams and provides compact scoreboard API

---

## 3) Memcached key map (current)

### Chat
- `clawbucket:chat:messages`
  - JSON list of chat messages

### Arm events
- `clawbucket:arm:events`
  - JSON list of arm toggle events

### RPS
- `clawbucket:rps:state`
  - JSON object with latest leader choice + metadata
- `clawbucket:rps:interval_seconds`
  - current configured round interval

### Player score state
- `clawbucket:rps:score:<task_id>`
  - integer score per player task
- `clawbucket:rps:last_seen:<task_id>`
  - last consumed RPS round id (timestamp) for idempotence

### Heartbeats
- `clawbucket:heartbeat:<task_name>`
  - heartbeat text (`Ping from ...`) with short TTL

> Note: this is simulation-grade state handling, not production-grade locking/transactions.

---

## 4) API reference

## Main app (`:8080`)

- `GET /api/swarm`
  - service replica state and tile metadata

- `POST /api/scale`
  - body: `{ "replicas": <int> }`

- `GET /api/chat`
- `POST /api/chat`
  - body: `{ "text": "..." }`

- `GET /api/arm/events`
- `POST /api/arm`
  - body: `{ "task_id": "...", "bot": "...", "state": "on|off" }`

- `GET /api/rps`
  - latest leader RPS state + configured interval

- `POST /api/rps/config`
  - body: `{ "interval_seconds": <int 2..120> }`

## Aggregator (`:8090`)

- `GET /healthz`
- `GET /api/scoreboard`
  - ARM scoreboard + `current_on_state`

---

## 5) UI behavior details

### Tile behavior
- One tile per running task
- Manager tile:
  - gold outline
  - `MANAGER` badge
  - no player score panel
- Non-manager tiles:
  - large score text
  - green for zero/positive
  - red for negative

### Arm controls
- Button text fixed as `Arm`
- Button green only when ON
- Top status pill green only when ON

### RPS controls
- Numeric interval input + apply button
- Changes are stored in Memcached and affect all tasks

---

## 6) Leader / manager election behavior in app

The app uses Swarm metadata to derive a single manager/publisher tile deterministically.

High-level rule:
1. Find Swarm leader node
2. Among running service tasks on that node, select lowest slot
3. If no match, fallback to first running task by slot

This same election logic is used to:
- mark `is_manager` in tile API data
- choose single RPS publisher

All other tasks are treated as players.

---

## 7) Deploy / update workflow

From repo root:

```bash
# build local image tag used by current stack
docker build -t mallond/clawbucket:latest .

# deploy/update stack
docker stack deploy -c docker-stack.yml clawbucket

# check services
docker service ls
docker service ps clawbucket_clawbucket
docker service ps clawbucket_clawbucket-aggregator
docker service ps clawbucket_memcached
docker service ps clawbucket_ollama
```

Open:
- `http://<host>:8080`
- `http://<host>:8090/api/scoreboard`
- `http://<host>:11434`

### Ollama model load + test

```bash
# find ollama container from the swarm service
OLLAMA_CID=$(docker ps --filter label=com.docker.swarm.service.name=clawbucket_ollama -q | head -n1)

# pull model into persistent ollama_data volume
docker exec -it "$OLLAMA_CID" ollama pull smollm2:135m

# list models to confirm

docker exec -it "$OLLAMA_CID" ollama list

# quick generation test

docker exec -it "$OLLAMA_CID" ollama run smollm2:135m "Reply with exactly OLLAMA_OK"
```

---

## 8) Troubleshooting notes

### 8080 serves old UI
- Ensure no extra local `python app.py` process is binding 8080 outside Swarm
- Hard refresh browser (Cmd+Shift+R)

### `memcached unavailable`
- Verify `clawbucket_memcached` service is `1/1`
- Confirm app and aggregator use correct host env (`MEMCACHED_HOST`)
- Verify image actually contains latest `app.py`/`aggregator.py`

### 8090 empty/404/failing
- Check aggregator service exists and is healthy
- Review logs:
  - `docker service logs clawbucket_clawbucket-aggregator`

### Scores not updating
- Confirm RPS state is changing via `GET /api/rps`
- Confirm one publisher exists and non-manager tiles are present
- Confirm player score keys are updating in Memcached

### Ollama service not ready / model not found
- Check service state: `docker service ps clawbucket_ollama`
- If service is still `Preparing`, wait for image/bootstrap completion
- Re-run pull from container:
  - `docker exec -it <ollama_cid> ollama pull smollm2:135m`
- Verify with: `docker exec -it <ollama_cid> ollama list`

---

## 9) Security / production disclaimer

This is intentionally simulation-first and **not production hardened**.

Current tradeoffs:
- Docker socket mounted into app service
- Memcached used as shared transient bus without auth
- No strict distributed locks/consensus in app layer

For production, plan to:
- split control-plane permissions
- add authn/authz
- isolate network paths
- move to durable state store and stronger coordination primitives

---

## 10) What to track next (for eventual SKILL.md extraction)

Potential sections for future `SKILL.md`:
- **Purpose / constraints** (simulation-first)
- **Service topology** (clawbucket, aggregator, memcached)
- **Key contracts** (Memcached schema + API contracts)
- **Election rules** (single manager/publisher rule)
- **Game loop semantics** (publisher, players, scoring)
- **UI invariants** (arm behavior, score styling)
- **Operational playbook** (deploy, verify, recover)
- **Known limitations** and hardening backlog

This README is now the canonical snapshot of current behavior.
