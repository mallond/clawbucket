# README-docker-stack.md



## Each service in one line

- **ollama** — one shared local LLM server, exposed on `11434`, with its model data persisted in `ollama_data`.
- **memcached** — one shared in-memory cache on `11211` for lightweight coordination/state.
- **clawbucket-aggregator** — one helper process running `aggregator.py`, exposed on `8090`, and pointed at Memcached.
- **clawbucket** — the main app service, exposed on `8080`, running **3 replicas**, using Memcached and Ollama, and mounted to the Docker socket.
- **clawbucket-b** — a second internal app service, also **3 replicas**, using the same Memcached and Ollama backend, but with no published host port.

## Super simple topology

```text
                        +--------------------+
                        |      ollama        |
                        |   :11434/api/...   |
                        +---------+----------+
                                  ^
                                  |
                                  |
+--------------------+            |            +--------------------+
|    clawbucket      |------------+------------|   clawbucket-b     |
|   3 replicas       |                         |   3 replicas       |
|   port 8080        |------------+------------|   internal only    |
+---------+----------+            |            +---------+----------+
          |                       |                      |
          |                       |                      |
          v                       v                      v
                 +-------------------------------+
                 |          memcached            |
                 |            :11211             |
                 +-------------------------------+
                                  ^
                                  |
                                  |
                     +------------+-------------+
                     |   clawbucket-aggregator  |
                     |        port 8090         |
                     +--------------------------+
