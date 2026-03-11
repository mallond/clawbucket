# clawbucket
<img width="1536" height="1024" alt="ChatGPT Image Mar 9, 2026, 01_47_20 PM" src="https://github.com/user-attachments/assets/879019f0-f51a-4e89-8e31-59749aaed52f" />

**Bob's World** is now a Docker Swarm simulation platform for coordinating many AI-enabled replicas.

> [!IMPORTANT]
> ## PRIME DIRECTIVE
> **This is an experimental build.** For operational steps, commands, and troubleshooting workflow, **ask OpenClaw / GPT-5.x for instructions first**.
> Try it — the agent works like having a **Human DevOps** partner on demand.

## Highlights (current)

- Swarm replica tiles with per-tile ARM state
- Leader/manager visualization and deterministic election
- Memcached-backed inter-container signaling
- Leader-published Rock/Paper/Scissors (RPS) rounds with per-task scoring
- Aggregator API for scoreboard + ON/OFF state
- **OpenClaw/PicoClaw collaboration model**:
  - each `clawbucket` task includes its **own local PicoClaw runtime/context**
  - all tasks share a single **Ollama** backend for local model inference/fallback
- Starship Troopers-style generation prompts (unit flavor)

---

## Vocabulary - Talk to your Bot, It can do all this and more. Ask and you will Recive.  
* **Swarm** — A cluster of Docker engines operating together as one orchestration system.
* **Node** — A machine participating in the swarm.
* **Manager node** — A node that maintains cluster state and makes orchestration decisions.
* **Worker node** — A node that runs tasks assigned by managers.
* **Leader** — The manager currently elected to coordinate swarm management through Raft.
* **Raft** — The consensus protocol managers use to keep swarm state consistent.
* **Quorum** — The minimum number of managers required to agree on cluster state changes.
* **Service** — The declarative definition of how containers should run in the swarm.
* **Task** — A single scheduled instance of a service container on a node.
* **Replica** — One desired running copy of a service.
* **Replicated service** — A service configured to run a specified number of replicas.
* **Global service** — A service configured to run exactly one task on every eligible node.
* **Stack** — A group of services, networks, and configs deployed together, usually from a Compose file.
* **Desired state** — The target configuration the swarm tries to maintain.
* **Actual state** — The real current condition of services and tasks in the cluster.
* **Reconciliation** — The manager process that continuously adjusts actual state toward desired state.
* **Scheduler** — The swarm component that decides where tasks should be placed.
* **Placement constraint** — A hard rule limiting which nodes may run a task.
* **Placement preference** — A soft rule influencing task distribution across nodes.
* **Label** — Metadata attached to nodes or objects for filtering and placement logic.
* **Availability** — A node state controlling whether it can receive tasks.
* **Active** — A node availability state allowing normal task scheduling.
* **Pause** — A node availability state preventing new tasks while leaving existing ones running.
* **Drain** — A node availability state that removes existing tasks and blocks new ones.
* **Overlay network** — A multi-host virtual network used for communication across swarm nodes.
* **Ingress network** — The special overlay network used for published service traffic and routing mesh.
* **Routing mesh** — The swarm traffic layer that accepts published port requests on any node and routes them to service tasks.
* **Publish port** — A port exposed externally by a swarm service.
* **Internal port** — The port the container listens on inside the service task.
* **Endpoint mode** — The method swarm uses to expose service discovery to clients.
* **VIP** — Virtual IP mode where a service gets a single internal IP for load-balanced access.
* **DNSRR** — DNS round-robin mode where service discovery returns multiple task IPs directly.
* **Slot** — The stable ordinal position of a replica within a replicated service.
* **Rolling update** — A controlled process for replacing service tasks with new versions incrementally.
* **Rollback** — Reverting a service to its previous configuration after an update issue.
* **Health check** — A container-level test used to determine whether a task is healthy.
* **Secret** — Sensitive data distributed securely to services at runtime.
* **Config** — Non-sensitive configuration data distributed to services by the swarm.
* **Join token** — A token used by new nodes to join the swarm as worker or manager.
* **Swarm CA** — The certificate authority that issues node certificates for swarm trust.
* **mTLS** — Mutual TLS used for encrypted and authenticated communication between swarm nodes.
* **Autolock** — A security feature that requires an unlock key after manager restart to access Raft data.
* **Unlock key** — The key required to unlock an autolocked manager.
* **Advertise address** — The network address a node tells other nodes to use for communication.
* **Listen address** — The local address a node binds to for swarm control traffic.
* **Dispatcher** — The manager component that assigns tasks and monitors worker status.
* **Allocator** — The manager component that assigns network and resource-related settings to swarm objects.
* **Control plane** — The management communication path for orchestration and cluster state.
* **Data plane** — The application traffic path used by running service containers.
* **Pending** — A task state indicating it has been accepted but not yet scheduled or started.
* **Running** — A task state indicating the container is currently executing.
* **Shutdown** — A task state indicating the task has been intentionally stopped.
* **Failed** — A task state indicating the task exited unexpectedly or could not start.
* **Orphaned task** — A task left behind or disconnected from expected management state, usually after failures or node issues.
* **Gossip** — The peer-to-peer mechanism used to distribute certain network state across nodes.
* **Swarm init** — The action that creates a new swarm and promotes the first node to manager.
* **Swarm join** — The action that adds a node to an existing swarm.
* **Swarm leave** — The action that removes a node from the swarm.
* **Swarm update** — The action that modifies swarm-wide settings.
* **Node promote** — The action that changes a worker into a manager.
* **Node demote** — The action that changes a manager into a worker.
* **Service scale** — The action of changing the number of replicas for a replicated service.
* **Service update** — The action of changing service configuration, image, ports, or placement rules.
* **Service inspect** — Viewing the detailed configuration and current state of a service.
* **Stack deploy** — Deploying a stack definition into the swarm.
* **Stack rm** — Removing a deployed stack and its swarm-managed resources.



```

```
## 1) Core idea

`clawbucket` runs as a replicated Swarm service. Each replica is both:

1. A participant in the simulation loop (heartbeat, RPS player/publisher, ARM events)
2. Its own lightweight AI agent runtime (local PicoClaw CLI + local context)

This allows **one-to-many scaling** of OpenClaw-style behavior:
- one container = one autonomous trooper
- many containers = coordinated AI squad/army

In short: **OpenClaw can run an army of one or many.**

<img width="796" height="688" alt="Screenshot 2026-03-10 122019" src="https://github.com/user-attachments/assets/61359cd3-072b-4e62-b8b4-faca957c103b" />

---

## 2) OpenClaw + PicoClaw collaboration design

### Before
A single shared `picoclaw` service handled generation requests for all tasks.

### Now
Each `clawbucket` task image embeds PicoClaw directly:

- `Dockerfile` copies `/usr/local/bin/picoclaw` from `sipeed/picoclaw:latest`
- task-local config at `/root/.picoclaw/config.json`
- `app.py` calls `picoclaw agent -m ...` via subprocess in the same container

### Why this matters

- **Isolated context per task** (no single shared chat/context window)
- Better swarm identity (each replica has its own voice/history/runtime state)
- Fewer cross-service hops for agent calls
- Shared model economics still preserved through common `ollama` service

This gives a practical hybrid:
- **distributed agents** at the edge (per task)
- **shared model backend** in the center (Ollama)

---

## 3) Services and topology

Defined in `docker-stack.yml`:

- `clawbucket`
  - Flask app (`app.py`) + local PicoClaw binary
  - Exposes `8080`
  - Mounts Docker socket for Swarm inspection/scale operations

- `clawbucket-aggregator`
  - Flask app (`aggregator.py`)
  - Exposes `8090`

- `memcached`
  - Shared transient state bus for coordination

- `ollama`
  - Shared model runtime
  - Exposes `11434`
  - Uses `ollama_data` volume for model persistence

> Note: standalone shared `picoclaw` service is no longer required for generation flow.

---

## 4) Current behavior

1. **Dashboard (`:8080`)**
   - shows replicas, slot, short task/node IDs, generated names
   - supports scaling via `/api/scale`

2. **ARM toggles**
   - per-tile arm button and ON/OFF visual state
   - emits Memcached event stream (`on`/`off`)

3. **Manager selection**
   - exactly one `MANAGER` tile
   - deterministic leader-aware selection

4. **Conversation + generated text**
   - chat panel backed by Memcached
   - per-task generated short phrases stored by task key
   - displayed text truncated to **50 chars**

5. **RPS loop**
   - single publisher task writes shared leader move
   - non-manager tasks score against leader move

6. **Haiku loop**
   - periodic generated haiku with PicoClaw-first, Ollama-fallback behavior

<img width="647" height="738" alt="image" src="https://github.com/user-attachments/assets/aba8c238-8c84-42aa-a0cd-33111a4e799b" />

---

## 5) Memcached keys (important)

### Chat
- `clawbucket:chat:messages`

### Arm events
- `clawbucket:arm:events`

### RPS
- `clawbucket:rps:state`
- `clawbucket:rps:interval_seconds`

### Scores
- `clawbucket:rps:score:<task_id>`
- `clawbucket:rps:last_seen:<task_id>`

### Heartbeats
- `clawbucket:heartbeat:<task_name>`

### Generated per-task text
- `clawbucket:picoclaw:threewords:<task_id>` (primary per-instance value)
- `clawbucket:picoclaw:threewords:latest` (shared latest snapshot)

---

## 6) API quick reference

Main app (`:8080`):
- `GET /api/swarm`
- `POST /api/scale`
- `GET /api/chat`
- `POST /api/chat`
- `GET /api/arm/events`
- `POST /api/arm`
- `GET /api/rps`
- `POST /api/rps/config`
- `GET /api/haiku`

Aggregator (`:8090`):
- `GET /healthz`
- `GET /api/scoreboard`

---

## 7) Deploy / update

```bash
# build image used by stack
docker build -t mallond/clawbucket:arm-agg-local .

# deploy/update
docker stack deploy -c docker-stack.yml clawbucket

# verify
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

---

## 8) Ollama model management

```bash
OLLAMA_CID=$(docker ps --filter label=com.docker.swarm.service.name=clawbucket_ollama -q | head -n1)

docker exec -it "$OLLAMA_CID" ollama pull smollm2:135m
docker exec -it "$OLLAMA_CID" ollama list
docker exec -it "$OLLAMA_CID" ollama run smollm2:135m "Reply with exactly OLLAMA_OK"
```

---

## 9) Troubleshooting

### Repeated/identical generated text on all tiles
- ensure per-task key path is active (`...threewords:<task_id>`)
- confirm new image is deployed to all replicas

### Missing APIs (404 on `/api/chat`, `/api/rps`, `/api/haiku`)
- indicates mixed old/new tasks during rollout
- wait for convergence or force update service

### Aggregator failing
- check logs: `docker service logs clawbucket_clawbucket-aggregator`
- ensure image includes `aggregator.py`

### Memcached issues
- verify `clawbucket_memcached` is healthy (1/1)

---

## 10) Safety and production note

This is simulation-first, not production-hardened.

Current tradeoffs include:
- Docker socket mount in app service
- unauthenticated Memcached bus
- lightweight coordination (not strict distributed consensus)

For production hardening: authn/authz, least-privilege control plane, stronger state/locking, and network isolation.

---

## 11) Unit flavor

Current prompt flavor supports a Starship Troopers-style military tone for generated chatter.

Unit motto:

> **Follow Me**

---

This README is the current canonical snapshot of behavior and architecture.

---

## 12) Simulated dual-rack mode (BOT 1 / BOT 2)

To run two isolated copies of the original design (rack-1 + rack-2) via Docker-in-Docker:

```bash
cd clawbucket
chmod +x deploy-racks.sh
./deploy-racks.sh
```

This starts two nested Docker hosts:
- `rack-1-dind` → stack `clawbucket` (BOT 1)
- `rack-2-dind` → stack `clawbucket` (BOT 2)

Host endpoints:
- BOT 1 dashboard: `http://localhost:18080`
- BOT 1 scoreboard: `http://localhost:18090/api/scoreboard`
- BOT 2 dashboard: `http://localhost:28080`
- BOT 2 scoreboard: `http://localhost:28090/api/scoreboard`

Files:
- `docker-compose.racks.yml` (boot both DinD racks)
- `deploy-racks.sh` (swarm init + stack deploy per rack)
