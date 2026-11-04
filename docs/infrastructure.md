# Kubernetes infrastructure

The development and integration-test resources are confined to `semantica-dataagent` and `semantica-dataagent-test`. Each has its own Neo4j and Milvus StatefulSet and local-path PVC; the development namespace also has a shared CPU embedding service. Milvus v2.5.27 uses embedded etcd and local storage. Embedding uses infinity 0.0.77 and BAAI/bge-m3 revision `5617a9f61b028005a4858fdac845db406aefb181`; its init container downloads the exact files and verifies SHA-256, and the service verifies again before every start. The expected hashes are from the supplied model manifest. No inference starts on a mismatch. Model files are mounted read-only for inference.

| Environment | Neo4j Bolt | Milvus | Embedding |
| --- | --- | --- | --- |
| dev | 31601 | 31602 | 31603 |
| test | 31611 | 31612 | dev service 31603 |

Use a cluster node address such as `172.16.242.124`. The PVCs use node-local storage and survive Pod restarts, but cannot automatically move their data to another node.

## Deploy

Apply `deploy/k8s/dev.yaml` and `deploy/k8s/test.yaml`, then create a separate Neo4j Secret in each namespace before starting Neo4j. Generate passwords locally and store them securely; do not commit them. For example, with kubectl configured for the intended cluster:

```sh
for ns in semantica-dataagent semantica-dataagent-test; do
  password=$(openssl rand -hex 24)
  kubectl -n "$ns" create secret generic neo4j-auth --from-literal="auth=neo4j/$password"
  unset password
done
kubectl apply -f deploy/k8s/embedding.yaml
```

Alternatively fill `deploy/k8s/neo4j-secret.template.yaml` outside version control and apply it for each namespace. Never overwrite an existing Secret without coordinating Neo4j's persisted credentials. The model download uses Hugging Face; set `HF_ENDPOINT` in the init container to `https://hf-mirror.com` if needed, then reapply. The revision and hashes stay unchanged.

## Connectivity

Install the pinned direct client dependencies from `scripts/requirements.txt` in a Python environment. Set `NEO4J_PASSWORD` to the password belonging to the selected namespace (the value after `neo4j/` in its Secret), then run:

```sh
NEO4J_PASSWORD='your-password' python scripts/check_infra.py dev
NEO4J_PASSWORD='your-test-password' python scripts/check_infra.py test
```

The script performs Cypher, creates and deletes a uniquely named Milvus collection, and checks a Chinese embedding for 1024 dimensions. `--skip-embedding` permits independent database diagnosis. Both environments use the same embedding service, which holds no per-environment data. Use isolated test instances for integration tests; the script creates temporary collections and deletes them even if a check fails.
