# Kubernetes infrastructure

The development and integration-test resources are confined to `semantica-dataagent` and `semantica-dataagent-test`. Each has its own Neo4j and Milvus StatefulSet and local-path PVC; each namespace also has its own CPU embedding service. Milvus v2.5.27 uses embedded etcd and local storage. Embedding uses infinity 0.0.77 and BAAI/bge-m3 revision `5617a9f61b028005a4858fdac845db406aefb181`; its init container downloads the exact files and verifies SHA-256, and the service verifies again before every start. The expected hashes are from the supplied model manifest. No inference starts on a mismatch. Model files are mounted read-only for inference.

| Environment | Neo4j Bolt | Milvus | Embedding |
| --- | --- | --- | --- |
| dev | 31601 | 31602 | 31603 |
| test | 31611 | 31612 | 31613 |

Use a cluster node address such as `172.16.242.124`. The PVCs use node-local storage and survive Pod restarts, but cannot automatically move their data to another node.

## Deploy

The test embedding deployment requires sufficient worker capacity; check scheduling headroom before applying the overlay.

Apply `deploy/k8s/dev.yaml` and `deploy/k8s/test.yaml`, then create a separate Neo4j Secret in each namespace before starting Neo4j. Generate passwords locally and store them securely; do not commit them. For example, with kubectl configured for the intended cluster:

```sh
for ns in semantica-dataagent semantica-dataagent-test; do
  password=$(openssl rand -hex 24)
  kubectl -n "$ns" create secret generic neo4j-auth --from-literal="auth=neo4j/$password"
  unset password
done
kubectl apply -f deploy/k8s/embedding.yaml
kubectl apply -k deploy/embedding-test
```

Alternatively fill `deploy/k8s/neo4j-secret.template.yaml` outside version control and apply it for each namespace. Never overwrite an existing Secret without coordinating Neo4j's persisted credentials. The model download uses Hugging Face; set `HF_ENDPOINT` in the init container to `https://hf-mirror.com` if needed, then reapply. The revision and hashes stay unchanged.

The embedding container sets `--batch-size 2`: [infinity 0.0.77](https://github.com/michaelfeil/infinity/blob/0.0.77/libs/infinity_emb/infinity_emb/inference/batch_handler.py) uses this as the maximum number of inputs in a dynamic inference batch, not the number of inputs permitted in an HTTP request. Larger requests are queued and processed in smaller batches. Its memory request is 2Gi and limit 4Gi; the request reflects the observed resident model footprint, and the limit leaves room for inference while staying below the 5Gi node budget. This trades throughput and latency for lower peak memory. Batch size alone does not bound memory for arbitrarily long inputs: the [pinned model's maximum sequence length](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/sentence_bert_config.json) is 8192 tokens, and the limit is not a guarantee for that maximum. Monitor OOMs, queue latency and node capacity when changing traffic or input lengths.

For a coordinated load test of the shared service, run `python3 scripts/stress_embedding.py` from WSL. It sends two requests of 32 distinct, approximately 1888-character Chinese inputs from each of three clients, samples `kubectl top` through SSH, and checks returned dimensions. It can interrupt other users and may cause an OOM with an unsafe configuration. Samples are periodic and can miss a short peak; compare Pod restart counts and termination reason before and after as well. Adjust `--namespace` along with `--url` when testing the test instance. Adjust `--url`, `--ssh`, `--clients`, `--batches`, `--chars`, and `--timeout` as needed. On 2026-11-04, `--timeout 600` completed 6/6 requests in 453.6 seconds with a sampled peak of 2053 MiB and no Pod restart. The old default batch size 32 OOMKilled on the same inputs; `--batch-size 1` avoided a restart but only 3/6 requests completed within a 900-second per-request timeout. Sampling can miss peaks and this test does not establish a bound for 8192-token inputs.

## Connectivity

Install the pinned direct client dependencies from `scripts/requirements.txt` in a Python environment. Set `NEO4J_PASSWORD` to the password belonging to the selected namespace (the value after `neo4j/` in its Secret), then run:

```sh
NEO4J_PASSWORD='your-password' python scripts/check_infra.py dev
NEO4J_PASSWORD='your-test-password' python scripts/check_infra.py test
```

The script performs Cypher, creates and deletes a uniquely named Milvus collection, and checks a Chinese embedding for 1024 dimensions. `--skip-embedding` permits independent database diagnosis. Each environment uses its own embedding service and model PVC. The test overlay prefers worker-02 and avoids worker-01 when scheduling; check node capacity before deployment. Use isolated test instances for integration tests; the script creates temporary collections and deletes them even if a check fails.
