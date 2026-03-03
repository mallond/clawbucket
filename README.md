# clawbucket

**Bob's World** — a tiny visual Docker Swarm dashboard.

It shows live replica chicklets (cards) with generated names, state, slot, task id, and node id.
It also includes a slider to scale replica count up/down.

## What makes it visual

- Every replica gets a deterministic generated name (e.g., `Neon Falcon`)
- Every replica has a stable color bar
- Dashboard auto-refreshes every 3 seconds
- You can watch chicklets appear/disappear as you scale

## Build and push image

```bash
docker build -t mallond/clawbucket:latest .
docker push mallond/clawbucket:latest
```

## Deploy to Swarm

```bash
docker swarm init   # once, if not already in swarm mode
docker stack deploy -c docker-stack.yml clawbucket
```

> Note: the stack mounts `/var/run/docker.sock` and constrains placement to a manager node so the app can query/scale the Swarm service.

## Use the dashboard

Open:

```text
http://<manager-or-node-ip>:8080
```

You should see **Bob's World** with:
- service status (`running / desired`)
- replica slider + `Scale Swarm` button
- replica chicklets for each running task

## Verify from CLI (optional)

```bash
docker service ls
docker service ps clawbucket_clawbucket
```

## Cleanup

```bash
docker stack rm clawbucket
```
