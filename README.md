# clawbucket

Simple visual QA app for Docker Swarm.

It serves a web page showing **which replica handled the request**:
- container hostname
- Swarm node hostname
- task name / slot / id
- stable color badge derived from hostname
- container start time

Refresh repeatedly and you should see these values rotate when traffic is balanced across replicas.

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

## Verify

```bash
docker service ls
docker service ps clawbucket_clawbucket
```

Open:

```text
http://<manager-or-node-ip>:8080
```

Then refresh the page many times for visual confirmation.

## Cleanup

```bash
docker stack rm clawbucket
```
