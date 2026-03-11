#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.racks.yml"
STACK_FILE="/workspace/clawbucket/docker-stack.yml"
IMAGE="mallond/clawbucket:arm-agg-local"

wait_for_docker() {
  local rack="$1"
  echo "[${rack}] waiting for nested Docker daemon..."
  until docker exec "$rack" docker info >/dev/null 2>&1; do
    sleep 1
  done
}

init_swarm_if_needed() {
  local rack="$1"
  local ip state control
  ip="$(docker exec "$rack" sh -lc "hostname -i | awk '{print \$1}'")"
  state="$(docker exec "$rack" docker info --format '{{.Swarm.LocalNodeState}}')"
  control="$(docker exec "$rack" docker info --format '{{.Swarm.ControlAvailable}}')"

  if [[ "$state" != "active" || "$control" != "true" ]]; then
    if [[ "$state" != "inactive" ]]; then
      docker exec "$rack" docker swarm leave --force >/dev/null 2>&1 || true
    fi
    echo "[${rack}] docker swarm init --advertise-addr ${ip}"
    docker exec "$rack" docker swarm init --advertise-addr "$ip" >/dev/null
  else
    echo "[${rack}] swarm already initialized"
  fi
}

deploy_stack() {
  local rack="$1"
  local stack="$2"

  echo "[${rack}] ensuring image ${IMAGE} is present"
  if ! docker exec "$rack" docker image inspect "$IMAGE" >/dev/null 2>&1; then
    if ! docker exec "$rack" docker pull "$IMAGE" >/dev/null 2>&1; then
      echo "[${rack}] remote image missing; building from local workspace"
      docker exec "$rack" docker build -t "$IMAGE" /workspace/clawbucket >/dev/null
    fi
  fi

  echo "[${rack}] deploying stack ${stack}"
  docker exec "$rack" docker stack deploy -c "$STACK_FILE" "$stack"
}

status() {
  local rack="$1"
  echo ""
  echo "=== ${rack} services ==="
  docker exec "$rack" docker service ls
}

cd "$ROOT_DIR"

echo "Bringing up simulated racks (bot-1 + bot-2)..."
docker compose -f "$COMPOSE_FILE" up -d

for rack in rack-1-dind rack-2-dind; do
  wait_for_docker "$rack"
  init_swarm_if_needed "$rack"
done

deploy_stack rack-1-dind clawbucket_rack1
deploy_stack rack-2-dind clawbucket_rack2

status rack-1-dind
status rack-2-dind

echo ""
echo "Ready. Endpoints:"
echo "- BOT 1 dashboard:   http://localhost:18080"
echo "- BOT 1 aggregator:  http://localhost:18090/api/scoreboard"
echo "- BOT 1 ollama:      http://localhost:18134"
echo "- BOT 2 dashboard:   http://localhost:28080"
echo "- BOT 2 aggregator:  http://localhost:28090/api/scoreboard"
echo "- BOT 2 ollama:      http://localhost:28134"
