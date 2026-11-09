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

The cluster embedding pool is a 4-replica StatefulSet (`embedding-pool`) behind the unchanged `embedding` Service on NodePort 31603. Pods keep `app: embedding`, so the old Deployment and the new StatefulSet can serve behind the same Service during a switch. Each replica gets its own 10Gi `local-path` volume through `volumeClaimTemplates` (volume name `model`); the init container downloads and verifies the pinned model into it, and the inference container mounts it read-only. `podManagementPolicy: Parallel` starts all four replicas at once, and preferred pod anti-affinity on `kubernetes.io/hostname` spreads them across nodes when capacity allows. A replica that cannot be placed waits for capacity rather than being pinned to a missing node. Each replica requests 2 CPU and 2Gi memory, with a 4Gi memory limit, so a full pool needs about 8 schedulable cores across the cluster.

Synthetic throughput from the small-scale benchmark that motivated this change (no real data):

| Configuration | Throughput (texts/s) |
| --- | --- |
| 1 replica, batch size 2 (previous production) | ~8.3 |
| 3 replicas, batch size 2 | 21.4 |
| 1 replica, batch size 16 | 13.7 |
| 4 replicas, batch size 16 (projected) | ~50 |

The StatefulSet uses `--batch-size 16`. [infinity 0.0.77](https://github.com/michaelfeil/infinity/blob/0.0.77/libs/infinity_emb/infinity_emb/inference/batch_handler.py) uses this as the maximum number of inputs in a dynamic inference batch, not the number of inputs permitted in an HTTP request; larger requests are queued and processed in smaller batches. Batch size 32 was not benchmarked for throughput and is not adopted. The older stress test below OOMKilled the single replica on approximately 1888-character inputs with the old default batch size 32; the 4Gi memory limit is not a guarantee for the [pinned model's maximum sequence length](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/sentence_bert_config.json) of 8192 tokens. Monitor OOMs, queue latency and node capacity when changing traffic or input lengths.

The recommended `embedding_concurrency` is 8 (allowed range 1–32). A single replica saturates at about 2–4; with four replicas the useful range is about 8–12, so 8 leaves headroom without adding queue latency, while raising concurrency on a single replica only increases waiting. Change it at runtime through the admin "plugin configuration / embedding request settings" UI or `PUT /api/v1/settings` with `{"values": {"embedding_concurrency": "8"}}`; the value is read when the next build starts, not during an in-flight build. A cold start (model download, SHA-256 verification and load) takes about 7 minutes. With `Parallel` management all four replicas start together, so the first rollout takes at least that long and the downloads may contend for bandwidth.

The local development compose services are unchanged: bge-m3 on 17997 keeps batch size 2 and the daily test model on 17998 keeps batch size 8. Those values are local conventions, not the cluster setting.

To switch from the old Deployment to the pool (coordinator only):

1. `kubectl apply -k deploy/k8s` creates StatefulSet `embedding-pool` while keeping Service `embedding`. `kubectl apply` does not delete the old `Deployment embedding` or the old PVC `embedding-model`.
2. `kubectl -n semantica-dataagent rollout status statefulset/embedding-pool --timeout=20m`, then `kubectl -n semantica-dataagent get pods -l app=embedding`. Wait until the four `embedding-pool-*` pods are Ready; during the overlap the old Deployment pod also appears.
3. Verify through the unchanged NodePort: `curl http://172.16.242.124:31603/health` and one `/embeddings` request, and confirm `kubectl -n semantica-dataagent get endpoints embedding` includes the new pods.
4. `kubectl -n semantica-dataagent delete deployment embedding`. The old pod leaves the Service while the four StatefulSet pods keep serving.
5. Recheck the endpoints and health, then confirm `kubectl -n semantica-dataagent get pvc` shows `model-embedding-pool-0` through `model-embedding-pool-3` and delete the now-unused old volume with `kubectl -n semantica-dataagent delete pvc embedding-model`. Never delete `embedding-model` before the old Deployment is gone.

To roll back before deleting `embedding-model`, re-apply the previous manifest from git history (`git show <previous-sha>:deploy/k8s/embedding.yaml | kubectl apply -f -`), wait for `kubectl -n semantica-dataagent rollout status deployment/embedding`, verify the NodePort, and then delete `statefulset/embedding-pool`. The old PVC still holds the model, so this path needs no download; the four `model-embedding-pool-*` PVCs remain until explicitly deleted. If `embedding-model` was already deleted, the rollback init container downloads the model again (about 7 minutes).

For a synthetic throughput curve without touching real data, run `scripts/embedding_bench.py` from the repository root. It sends random Chinese/English words, so it exercises the same request path without reading the source directory:

```sh
uv run --with httpx scripts/embedding_bench.py \
  http://172.16.242.124:31603 model/bge-m3 16,64 1,2,4,8 20
```

The arguments are the base URL, model, comma-separated batch sizes, comma-separated concurrency values, and optional seconds per combination (default 20). It prints texts/s, p50/p95 request latency and failure counts. Run short windows on the shared service and coordinate with other users; it is only a throughput curve and does not replace restart/OOM monitoring.

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

## Langfuse 观测服务

`deploy/k8s/langfuse/langfuse.yaml` 将固定版本 Langfuse 4.75.0、PostgreSQL、ClickHouse、Redis 和 MinIO 部署到 `semantica-dataagent-langfuse`，各存储使用 local-path PVC。Web 的 NodePort 为 `31631`，地址 `http://172.16.242.124:31631`。这是单副本部署；节点和卷故障会中断观测服务。

首次安装在有 SSH 授权的机器运行 `uv run --directory backend python ../deploy/k8s/langfuse/initialize.py`。脚本在已有命名空间保留 Secret 并重用现有项目；首次通过标准输入创建 Secret，先初始化 `dataagent-test`，再初始化 `dataagent-dev`。不要把 Secret 内容写入文件或日志。项目密钥分别保存在命名空间内的 `langfuse-test-keys` 与 `langfuse-dev-keys`；初始登录密码保存在 `langfuse-config`。常规更新使用 `kubectl apply -f deploy/k8s/langfuse/langfuse.yaml`，保留 PVC 和 Secret。删除 PVC 会删除观测数据；重新初始化前应备份并明确处理已有命名空间。

管理员可在插件设置页开启 Langfuse 并填写地址和项目公钥、私钥；私钥加密存储且界面不回显，留空保留已有值。上报使用锁文件固定的 httpx，通过有界队列异步发送，超时或发送失败不会中断请求。允许上报的应用字段集中列在 `telemetry.py` 的 `ALLOWED_FIELDS`，包括追踪标识、agent、provider、模型、工具名称、参数、结果摘要、耗时、SQL 校验和回答；其他字段会被丢弃。

同一 Pod 内 ClickHouse 原生协议监听 9001，MinIO 监听 9000；六个容器的启动和就绪检查只使用 TCP 或 HTTP，不依赖容器内 exec。启动脚本通过 NodePort 健康端点等待 Web 初始化。

## DataAgent 应用和会话 Pod

应用清单位于 `deploy/k8s/app/`，前端和本地 MCP 共用 `http://172.16.242.124:31630`，MCP 地址为 `/mcp/`。集群内应用 Service 是 `dataagent:8000`。应用单副本使用 `dataagent-data` PVC 保存 SQLite、源目录和会话文件；默认 `local-path` 是节点本地 RWO 卷。每轮将该对话的工作目录和会话文件复制到会话 Pod 的临时卷，并在结束时复制回来。轮次结束时删除 Pod，重启应用时按会话标签清理遗留 Pod；应用节点不可用时本地卷不会自动迁移。应用的 ServiceAccount 只在独立会话命名空间操作 Pod、日志和 exec。

应用和 runner 分别使用 `172.16.242.124:31640/dataagent-app:<master 短 sha>`（当前 `2bcd111`） 和 `172.16.242.124:31640/dataagent-agent-runner:1.2.1`。仓库是集群内 HTTP registry，无认证；节点 containerd 的信任配置由维护者管理。从 WSL 构建时运行 `docker build -f deploy/k8s/app/Dockerfile -t semantica-dataagent-app:<master 短 sha> .` 和 `docker build -t semantica-dataagent-agent-runner:1.2.1 deploy/agent-runner`，分别 `docker save` 到磁盘 tar，再用固定版本 crane 的 `push --insecure` 推送；不要修改 Docker Desktop 配置。crane v0.20.3 的 Linux x86_64 发布包 SHA-256 为 `36c67a932f489b3f2724b64af90b599a8ef2aa7b004872597373c0ad694dc059`。镜像基础层按 digest 固定；发布前对镜像进行真实集群拉取验证。

从旧版升级时，先在会话命名空间部署新的 Role 和 RoleBinding，然后删除旧应用命名空间的同名权限：`kubectl -n semantica-dataagent delete rolebinding dataagent-sessions --ignore-not-found` 和 `kubectl -n semantica-dataagent delete role dataagent-sessions --ignore-not-found`。使用 `kubectl -n semantica-dataagent auth can-i create pods/exec --as=system:serviceaccount:semantica-dataagent:dataagent` 确认返回 `no`，并在会话命名空间确认返回 `yes`。仅 `kubectl apply -k` 不会删除旧权限。

首次安装先创建 `dataagent-encryption` Secret（`key` 为新生成的 Fernet 密钥），不要覆盖已有密钥或 PVC。`kubectl apply -f deploy/k8s/app/bootstrap.yaml` 后等待 `dataagent-bootstrap` 就绪，在其中执行 `semantica-dataagent init` 并安全保存只显示一次的管理员 token；将源文件放到 PVC 的 `/data/source`。删除 bootstrap Pod 后 `kubectl apply -k deploy/k8s/app`。初始化只对空数据库执行；升级不能重新初始化数据库。应用从本命名空间现有 `neo4j-auth` Secret 的 `auth` 环境变量取得开发 Neo4j 凭据，只有用户名与设置一致且没有显式设置密码时使用；用户在设置中保存的密码优先。设置 Neo4j `bolt://neo4j:7687`、Milvus `http://milvus:19530`、embedding `http://embedding:7997` 和源目录 `/data/source`，在前端配置 provider 并触发全量构建。API key 加密存储，避免将密钥写入命令、镜像或日志。

每轮由应用解析 provider 和 MCP 的主机名，写入会话 Pod 的 hostAliases；init 容器安装 IPv4/IPv6 默认拒绝规则，仅允许解析出的 IP 和端口，不允许 DNS 出站。会话 Pod 默认在 `semantica-dataagent-sessions`，可通过 `DATAAGENT_SESSION_NAMESPACE` 配置；对应命名空间需要部署 Role 和 RoleBinding。会话目录通过应用与 Pod 之间的归档传递，Pod 使用临时卷；应用 ServiceAccount 仅在会话命名空间具有 Pod 权限。agent 容器非 root、只读根文件系统、丢弃全部 capability。集群 flannel 不执行 NetworkPolicy，实际限制由 iptables 执行。

升级先推送新镜像并更新清单中的镜像引用，再 `kubectl apply -k deploy/k8s/app`，验证首页、MCP 和会话出站；单副本 Recreate 会短暂中断服务。`serve` 启动前会对 `DATAAGENT_DB_PATH`（集群内为 `/data/dataagent.db`）自动执行 `alembic upgrade head`，并在日志中打印数据库路径、迁移前后版本和备份路径；已是 head 时不备份也不迁移。迁移前如果版本不是 head，应用先把 SQLite 备份到同目录的 `/data/dataagent.db.bak-<UTC 时间戳>`，只保留最近 3 份。不要删除这些备份。也可以先在 `DATAAGENT_DB_PATH` 上手动运行 `semantica-dataagent migrate` 预览和升级。迁移失败时 `serve` 以非 0 退出且不监听端口，日志会写明数据库路径、当前版本和失败原因；排障后需要修复数据库或恢复迁移前备份再启动。

回滚到旧镜像前必须恢复迁移前的数据库备份：旧代码不认识新的 alembic 版本号，直接把镜像引用改回上一版会让旧应用读到新 schema 并启动失败。回滚时先让应用停止（把 Deployment 缩到 0，或保持已经因迁移失败而退出的 Pod），把本次升级前的 `/data/dataagent.db.bak-<UTC 时间戳>` 复制回 `/data/dataagent.db`，删除同目录残留的 `dataagent.db-wal`、`dataagent.db-shm`，再 apply 旧镜像引用；保留 PVC 和加密 Secret。全量知识构建和当前代切换按现有构建 API 执行，回滚知识使用保留代切换，镜像回滚不会自动切换知识代。

## 项目隔离与目录

项目共享同一套 Neo4j、Milvus 与 embedding 服务，但知识代名前缀为 `<项目 id>_`，Milvus 每代一个 `sda_<代名>` 集合；每代的 Neo4j 节点仍以 `generation` 属性隔离。项目 id 由系统生成（名称转小写英文数字；没有 ASCII 字母数字时回退为 `project`、`project2`……；冲突加后缀），只含小写字母和数字，最长 24 字符。清理任务只处理本项目的代。

普通项目的源目录由 `DATAAGENT_PROJECTS_ROOT` 决定：设置时取 `$DATAAGENT_PROJECTS_ROOT/<项目 id>/source`，未设置时取 `DATAAGENT_DB_PATH` 所在目录下的 `projects/<项目 id>/source`。集群内数据库在 `/data/dataagent.db`，因此默认落在 `/data/projects/<项目 id>/source`（同一数据卷）；默认项目仍使用设置里的 `/data/source`。创建项目时会建好源目录，git 仓库在第一次上传时按现有逻辑初始化。构建队列仍只有一个工作线程，项目之间串行。

2026-11-05 的部署验收使用 `samples/demo` 初始化 `/data/source`，首次全量构建 56.5 秒（20 张表、15 篇文档）；这只是示例知识，接入实际业务前由管理员上传并全量构建真实源目录。验收脚本 `backend/tests/e2e/k8s/verify.py` 从 `DATAAGENT_TOKEN` 环境变量取令牌，检查内网首页、pi 问答的 SQL 和引用、轮次后 Pod 清理以及本地 MCP 检索；出站限制使用票专属 harness 检查复验。不要把令牌写进命令历史或日志。


## 直接运行与会话运行时

按 `docs/code-standards.md` 的“运行应用”初始化并启动服务。默认 `DATAAGENT_AGENT_MODE=sandbox` 使用 Docker；先构建 `docker build -t semantica-dataagent-agent-runner:1.2.0 deploy/agent-runner`。也可显式设置 `DATAAGENT_AGENT_MODE=docker`。Docker 会话使用 internal 网络和仅转发模型及 MCP 的中继；目标在每轮解析，容器通过 hosts 访问中继。可信开发调试可设置 `DATAAGENT_AGENT_MODE=local` 使用本机进程，该模式没有隔离。Kubernetes 使用 `DATAAGENT_AGENT_MODE=k8s`，部署方式见上文。
