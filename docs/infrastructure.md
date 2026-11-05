# Infrastructure

The development and integration-test resources are confined to `semantica-dataagent` and `semantica-dataagent-test`. Each has its own Neo4j and Milvus StatefulSet and local-path PVC; development uses a CPU embedding service, and tests use two local Docker instances. Milvus v2.5.27 uses embedded etcd and local storage. Daily integration tests use BAAI/bge-small-zh-v1.5; BAAI/bge-m3 is reserved for real-sample builds and acceptance evaluation. Both models use infinity 0.0.77 and the same `deploy/k8s/model.py`: each pinned revision is downloaded with its exact file list, every file's SHA-256 is verified, and the service verifies again before every start. No inference starts on a mismatch. Model files are mounted read-only for inference. The bge-m3 revision is `5617a9f61b028005a4858fdac845db406aefb181`; the small-model revision is `7999e1d3359715c523056ef9478215996d62a620`.

| Environment | Neo4j Bolt | Milvus | Embedding |
| --- | --- | --- | --- |
| dev | 31601 | 31602 | 31603 |
| test (daily) | 31611 | 31612 | 127.0.0.1:17998 (bge-small-zh-v1.5, local Docker) |
| test (real samples / acceptance) | 31611 | 31612 | 127.0.0.1:17997 (bge-m3, local Docker) |
| test2（第二套测试存储，`semantica-dataagent-test2`） | 31621 | 31622 | 同上两个本机 embedding |

两套测试存储互相独立，供并行的测试各用一套，避免多张票同时写同一套存储时互相干扰（见 #44）。协调者在测试任务书里指定使用哪一套；只用一套时默认 test。`deploy/k8s/test2.yaml` 与 `test.yaml` 只有命名空间和端口不同，Neo4j Secret 同样在 `semantica-dataagent-test2` 里单独创建。

Use a cluster node address such as `172.16.242.124`. The PVCs use node-local storage and survive Pod restarts, but cannot automatically move their data to another node.

## Deploy

Apply `deploy/k8s/dev.yaml`, `deploy/k8s/test.yaml` and `deploy/k8s/test2.yaml`, then create a separate Neo4j Secret in each namespace before starting Neo4j. Generate passwords locally and store them securely; do not commit them. For example, with kubectl configured for the intended cluster:

```sh
for ns in semantica-dataagent semantica-dataagent-test semantica-dataagent-test2; do
  password=$(openssl rand -hex 24)
  kubectl -n "$ns" create secret generic neo4j-auth --from-literal="auth=neo4j/$password"
  unset password
done
kubectl apply -k deploy/k8s
```

Alternatively fill `deploy/k8s/neo4j-secret.template.yaml` outside version control and apply it for each namespace. Never overwrite an existing Secret without coordinating Neo4j's persisted credentials. The model download uses Hugging Face; set `HF_ENDPOINT` in the init container to `https://hf-mirror.com` if needed, then reapply. The revision and hashes stay unchanged.

The embedding container sets `--batch-size 2`: [infinity 0.0.77](https://github.com/michaelfeil/infinity/blob/0.0.77/libs/infinity_emb/infinity_emb/inference/batch_handler.py) uses this as the maximum number of inputs in a dynamic inference batch, not the number of inputs permitted in an HTTP request. Larger requests are queued and processed in smaller batches. Its memory request is 2Gi and limit 4Gi; the request reflects the observed resident model footprint, and the limit leaves room for inference while staying below the 5Gi node budget. This trades throughput and latency for lower peak memory. Batch size alone does not bound memory for arbitrarily long inputs: the [pinned model's maximum sequence length](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/sentence_bert_config.json) is 8192 tokens, and the limit is not a guarantee for that maximum. Monitor OOMs, queue latency and node capacity when changing traffic or input lengths.

For a coordinated load test of the shared service, run `python3 scripts/stress_embedding.py` from WSL. It sends two requests of 32 distinct, approximately 1888-character Chinese inputs from each of three clients, samples `kubectl top` through SSH, and checks returned dimensions. It can interrupt other users and may cause an OOM with an unsafe configuration. Samples are periodic and can miss a short peak; compare Pod restart counts and termination reason before and after as well. Adjust `--url`, `--ssh`, `--clients`, `--batches`, `--chars`, and `--timeout` as needed. On 2026-11-04, `--timeout 600` completed 6/6 requests in 453.6 seconds with a sampled peak of 2053 MiB and no Pod restart. The old default batch size 32 OOMKilled on the same inputs; `--batch-size 1` avoided a restart but only 3/6 requests completed within a 900-second per-request timeout. Sampling can miss peaks and this test does not establish a bound for 8192-token inputs.

## Local test embedding

From the repository root, start and inspect only the daily small-model service:

```sh
docker compose -f deploy/embedding-compose.yaml up -d embedding-small
docker compose -f deploy/embedding-compose.yaml ps
docker compose -f deploy/embedding-compose.yaml logs -f embedding-small
```

`embedding-small` uses port 17998, batch size 8, a 2 GiB memory limit, and the fixed bge-small-zh-v1.5 revision above. Its default model directory is `~/.cache/semantica-dataagent/bge-small-zh-v1.5`, outside the repository; set `EMBEDDING_SMALL_MODEL_DIR` to an absolute path to override it. The first start downloads and verifies the pinned files; each later start verifies them again before inference. The model and the shared integrity script are mounted read-only. On 2026-11-05, 24 concurrent requests of 8 approximately 900-character Chinese inputs sampled a peak of about 1.0 GiB with no restart; 2 GiB leaves headroom while staying well below bge-m3's 4 GiB limit.

The existing `embedding` service on 17997 is the bge-m3 instance used by `scripts/verify_sample_build.py` and acceptance checks. Do not stop, recreate, or otherwise modify that container during routine test work. To stop the daily service without touching bge-m3, use `docker compose -f deploy/embedding-compose.yaml stop embedding-small`; do not use `down`.

## Connectivity

Install the pinned direct client dependencies from `scripts/requirements.txt` in a Python environment. Set `NEO4J_PASSWORD` to the password belonging to the selected namespace (the value after `neo4j/` in its Secret), then run:

```sh
NEO4J_PASSWORD='your-password' python scripts/check_infra.py dev
NEO4J_PASSWORD='your-test-password' python scripts/check_infra.py test
```

The script performs Cypher, creates and deletes a uniquely named Milvus collection, and checks a Chinese embedding returned by the selected model (512 dimensions for the daily small model, 1024 for dev bge-m3). `--skip-embedding` permits independent database diagnosis. The test embedding runs locally and has its own model directory. Use isolated test instances for integration tests; the script creates temporary collections and deletes them even if a check fails.
