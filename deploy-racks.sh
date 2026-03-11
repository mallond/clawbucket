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
  local ip pool
  ip="$(docker exec "$rack" sh -lc "hostname -i | awk '{print \$1}'")"

  # Force a clean one-node swarm with a rack-specific address pool.
  # This avoids overlay pool collisions in DinD simulation.
  docker exec "$rack" docker swarm leave --force >/dev/null 2>&1 || true

  if [[ "$rack" == "rack-1-dind" ]]; then
    pool="10.41.0.0/16"
  else
    pool="10.42.0.0/16"
  fi

  echo "[${rack}] docker swarm init --advertise-addr ${ip} --default-addr-pool ${pool}"
  docker exec "$rack" docker swarm init \
    --advertise-addr "$ip" \
    --default-addr-pool "$pool" \
    --default-addr-pool-mask-length 24 >/dev/null
}

deploy_stack() {
  local rack="$1"
  local stack="$2"
  local bot_label="$3"

  # Clean up old rack-specific stacks from earlier iterations.
  docker exec "$rack" docker stack rm clawbucket_rack1 >/dev/null 2>&1 || true
  docker exec "$rack" docker stack rm clawbucket_rack2 >/dev/null 2>&1 || true

  echo "[${rack}] building latest image ${IMAGE} from local workspace"
  docker exec "$rack" docker build -t "$IMAGE" /workspace/clawbucket >/dev/null

  echo "[${rack}] deploying stack ${stack} (${bot_label})"
  docker exec "$rack" sh -lc "DASHBOARD_BOT_LABEL='${bot_label}' docker stack deploy -c '$STACK_FILE' '$stack'"
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

# Use the original stack name inside each isolated rack so service env names resolve.
deploy_stack rack-1-dind clawbucket "Machine Rack 1"
deploy_stack rack-2-dind clawbucket "Machine Rack 2"

status rack-1-dind
status rack-2-dind

echo ""
echo "Ready. Endpoints:"
echo "- Machine Rack 1 dashboard:   http://localhost:18080"
echo "- Machine Rack 1 aggregator:  http://localhost:18090/api/scoreboard"
echo "- Machine Rack 1 ollama:      http://localhost:18134"
echo "- Machine Rack 2 dashboard:   http://localhost:28080"
echo "- Machine Rack 2 aggregator:  http://localhost:28090/api/scoreboard"
echo "- Machine Rack 2 ollama:      http://localhost:28134"
