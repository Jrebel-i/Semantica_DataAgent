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

## Langfuse 观测服务

`deploy/k8s/langfuse/langfuse.yaml` 将固定版本 Langfuse 4.75.0、PostgreSQL、ClickHouse、Redis 和 MinIO 部署到 `semantica-dataagent-langfuse`，各存储使用 local-path PVC。Web 的 NodePort 为 `31631`，地址 `http://172.16.242.124:31631`。这是单副本部署；节点和卷故障会中断观测服务。

首次安装在有 SSH 授权的机器运行 `uv run --directory backend python ../deploy/k8s/langfuse/initialize.py`。脚本在已有命名空间保留 Secret 并重用现有项目；首次通过标准输入创建 Secret，先初始化 `dataagent-test`，再初始化 `dataagent-dev`。不要把 Secret 内容写入文件或日志。项目密钥分别保存在命名空间内的 `langfuse-test-keys` 与 `langfuse-dev-keys`；初始登录密码保存在 `langfuse-config`。常规更新使用 `kubectl apply -f deploy/k8s/langfuse/langfuse.yaml`，保留 PVC 和 Secret。删除 PVC 会删除观测数据；重新初始化前应备份并明确处理已有命名空间。

管理员可在插件设置页开启 Langfuse 并填写地址和项目公钥、私钥；私钥加密存储且界面不回显，留空保留已有值。上报使用锁文件固定的 httpx，通过有界队列异步发送，超时或发送失败不会中断请求。允许上报的应用字段集中列在 `telemetry.py` 的 `ALLOWED_FIELDS`，包括追踪标识、agent、provider、模型、工具名称、参数、结果摘要、耗时、SQL 校验和回答；其他字段会被丢弃。

同一 Pod 内 ClickHouse 原生协议监听 9001，MinIO 监听 9000；六个容器的启动和就绪检查只使用 TCP 或 HTTP，不依赖容器内 exec。启动脚本通过 NodePort 健康端点等待 Web 初始化。

## DataAgent 应用和会话 Pod

应用清单位于 `deploy/k8s/app/`，前端和本地 MCP 共用 `http://172.16.242.124:31630`，MCP 地址为 `/mcp/`。集群内应用 Service 是 `dataagent:8000`。应用单副本使用 `dataagent-data` PVC 保存 SQLite、源目录和会话文件；默认 `local-path` 是节点本地 RWO 卷。会话 Pod 限定在应用所在节点，并只以子路径挂载本对话的工作目录和会话文件。轮次结束时删除 Pod，重启应用时按会话标签清理遗留 Pod；节点不可用时本地卷不会自动迁移。应用的 ServiceAccount 只在本命名空间操作 Pod、日志和 exec。

应用和 runner 分别使用 `172.16.242.124:31640/dataagent-app:issue-26` 和 `172.16.242.124:31640/dataagent-agent-runner:1.2.1`。仓库是集群内 HTTP registry，无认证；节点 containerd 的信任配置由维护者管理。从 WSL 构建时运行 `docker build -f deploy/k8s/app/Dockerfile -t semantica-dataagent-app:issue-26 .` 和 `docker build -t semantica-dataagent-agent-runner:1.2.1 deploy/agent-runner`，分别 `docker save` 到磁盘 tar，再用固定版本 crane 的 `push --insecure` 推送；不要修改 Docker Desktop 配置。crane v0.20.3 的 Linux x86_64 发布包 SHA-256 为 `36c67a932f489b3f2724b64af90b599a8ef2aa7b004872597373c0ad694dc059`。镜像基础层按 digest 固定；发布前对镜像进行真实集群拉取验证。

首次安装先创建 `dataagent-encryption` Secret（`key` 为新生成的 Fernet 密钥），不要覆盖已有密钥或 PVC。`kubectl apply -f deploy/k8s/app/bootstrap.yaml` 后等待 `dataagent-bootstrap` 就绪，在其中执行 `semantica-dataagent init` 并安全保存只显示一次的管理员 token；将源文件放到 PVC 的 `/data/source`。删除 bootstrap Pod 后 `kubectl apply -k deploy/k8s/app`。初始化只对空数据库执行；升级不能重新初始化数据库。应用从本命名空间现有 `neo4j-auth` Secret 的 `auth` 环境变量取得开发 Neo4j 凭据，只有用户名与设置一致且没有显式设置密码时使用；用户在设置中保存的密码优先。设置 Neo4j `bolt://neo4j:7687`、Milvus `http://milvus:19530`、embedding `http://embedding:7997` 和源目录 `/data/source`，在前端配置 provider 并触发全量构建。API key 加密存储，避免将密钥写入命令、镜像或日志。

会话 Pod 的 init 容器解析当前 provider 和 MCP 地址，安装 IPv4/IPv6 iptables 默认拒绝规则，只允许到解析出的目标 IP 和端口，以及到 Pod 的集群 DNS 的 TCP/UDP 53；允许已建立连接的返回流量。agent 容器非 root、只读根文件系统、丢弃全部 capability，无法改规则。地址在每轮 init 时解析，DNS 仍可访问集群 DNS；模型网关地址改变时新轮次重新解析。`NetworkPolicy` 同时限制会话 Pod，但本集群只有 flannel，**目前不执行 NetworkPolicy**，实际限制由 iptables 执行。标准 NetworkPolicy 无法按 FQDN 表达动态 provider，当前对外 TCP 443 较宽，iptables 仍限定到解析后的地址；启用策略引擎或使用其他 provider 端口前需相应更新策略并重新验证。

升级先推送新镜像并更新清单中的镜像引用，再 `kubectl apply -k deploy/k8s/app`，验证首页、MCP 和会话出站；单副本 Recreate 会短暂中断服务。回滚使用上一版镜像引用重新 apply，保留 PVC 和加密 Secret；不可删除 PVC 来回滚。全量知识构建和当前代切换按现有构建 API 执行，回滚知识使用保留代切换，镜像回滚不会自动切换知识代。

2026-11-05 的部署验收使用 `samples/demo` 初始化 `/data/source`，首次全量构建 56.5 秒（20 张表、15 篇文档）；这只是示例知识，接入实际业务前由管理员上传并全量构建真实源目录。验收脚本 `backend/tests/e2e/k8s/verify.py` 从 `DATAAGENT_TOKEN` 环境变量取令牌，检查内网首页、pi 问答的 SQL 和引用、轮次后 Pod 清理以及本地 MCP 检索；出站限制使用票专属 harness 检查复验。不要把令牌写进命令历史或日志。
