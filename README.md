# clawbucket

**Bob's World** — a tiny visual Docker Swarm dashboard.

It shows live replica chicklets (cards) with generated names, state, slot, task id, and node id. It also includes a slider to scale replica count up/down.

---

## What this is

- A single web app on port **8080**
- Runs as a Docker Swarm service
- Uses Docker API (via mounted socket) to:
  - read Swarm task state
  - scale service replicas from the UI

Dashboard title: **🌍 Bob's World**

---

## Prerequisites (on the target machine)

- Docker Engine installed
- User can run Docker commands (`docker ps` works)
- Port **8080** open in firewall/router/security group

Optional but recommended:
- A DNS name pointing to the host IP
- TLS termination via reverse proxy (Nginx/Traefik/Caddy)

---

## 1) Get the code

```bash
git clone https://github.com/mallond/clawbucket.git
cd clawbucket
```

---

## 2) Build and push image

Use your own Docker Hub namespace if needed.

```bash
docker build -t mallond/clawbucket:latest .
docker push mallond/clawbucket:latest
```

If you changed image name, update `docker-stack.yml` accordingly.

---

## 3) Initialize Swarm (manager)

On the manager node:

```bash
docker swarm init
```

If already initialized, Docker will say so — that's fine.

Check role:

```bash
docker info | grep -E "Swarm|Is Manager"
```

You want:
- `Swarm: active`
- `Is Manager: true`

---

## 4) Deploy Bob's World

From the manager node, inside repo:

```bash
docker stack deploy -c docker-stack.yml clawbucket
```

Watch rollout:

```bash
docker service ls
docker service ps clawbucket_clawbucket
```

---

## 5) Open dashboard

```text
http://<manager-or-node-ip>:8080
```

You should see:
- service status (`running / desired`)
- replica slider + **Scale Swarm** button
- replica chicklets (generated names + task/node info)

---

## 6) Share with a friend (production-ish quick path)

### A) Network access

Allow TCP **8080** inbound to the manager/node IP.

### B) Give friend URL

- `http://<public-ip>:8080`
- or `http://<your-domain>:8080`

### C) Basic health checks

```bash
curl -s http://127.0.0.1:8080/api/swarm | jq
curl -s http://127.0.0.1:8080/api/whoami | jq
```

If `jq` isn't installed:

```bash
curl -s http://127.0.0.1:8080/api/swarm
```

---

## Multi-node Swarm (optional)

On manager, get worker join command:

```bash
docker swarm join-token worker
```

Run printed `docker swarm join ...` on each worker.

Check nodes on manager:

```bash
docker node ls
```

---

## Updating to latest version

```bash
cd clawbucket
git pull
docker build -t mallond/clawbucket:latest .
docker push mallond/clawbucket:latest
docker stack deploy -c docker-stack.yml clawbucket
```

---

## Troubleshooting

### Error: `this node is not a swarm manager`
Run deploy on the manager node, or initialize this node as manager:

```bash
docker swarm init
```

### Browser shows `ERR_EMPTY_RESPONSE` during scaling
This can happen briefly while tasks are replaced. Current build includes retry logic; refresh and it should recover.

### Service not found in dashboard API
Ensure stack/service name is exactly:

```text
clawbucket_clawbucket
```

Check:

```bash
docker service ls
```

### Port unreachable externally
- verify host firewall/security group allows 8080
- verify router/NAT forwards 8080 to host (if behind home router)

---

## Security note

This demo mounts Docker socket into the app container (`/var/run/docker.sock`), which gives powerful control over Docker on that host. Good for controlled experiments; not ideal for hardened public production.

Recommended hardening for production:

- Split services:
  - **dashboard-ui** (public, read-only, no Docker socket)
  - **swarm-controller** (private/internal, handles scale operations)
- Protect scale actions with authentication + authorization (RBAC), rate limiting, and audit logs.
- Isolate network paths:
  - expose only UI through reverse proxy with TLS
  - keep controller on private/internal overlay network
  - optionally restrict admin routes by IP allowlist/VPN
- Apply least privilege:
  - use a Docker socket proxy with only required endpoints
  - run containers non-root, read-only filesystem, dropped capabilities, `no-new-privileges`
- Add server-side safety rails:
  - enforce min/max replica bounds
  - cooldown between scale operations

For safer architecture, split control plane and app plane, add auth, and avoid exposing Docker socket to internet-facing services.

---

## Remove stack

```bash
docker stack rm clawbucket
```
