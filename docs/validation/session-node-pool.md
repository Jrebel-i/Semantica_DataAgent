# 会话节点池验证归档

## 范围与结论

本票为 Kubernetes 会话 Pod 增加可配置的允许节点池：设置 `DATAAGENT_SESSION_NODES` 时使用硬性
`nodeAffinity` 限定 `kubernetes.io/hostname`，由调度器在池内选择；未设置时保留
`DATAAGENT_NODE_NAME` 的单节点 `nodeSelector` 旧行为。生产清单只允许已验证的
`k8s-worker-03,k8s-worker-05`。空串、空项、空白、非法节点名和重复 env 均失败关闭。

最终返工只修改 e2e 与测试，修正 e2e 无条件给会话 Pod 追加应用节点 `nodeSelector`、与节点池
required affinity 冲突的问题。运行代码与 `9c0b7a4` 一致，没有新增运行逻辑。

## Rebase 与补丁一致性

- 原工单基线：`001d46df8eda241a6347066b7772389bba7dfba9`
- Rebase 时主干：`075a85d0e9107e0579f00c9c0d1373814cd8f7e6`
- Rebase 结果：无冲突，提交重写为 `9c0b7a4` → `1aa45f5`、`f2adfe3` → `00ed580`
- `git range-diff 001d46d..f2adfe3 origin/master..HEAD`：两个提交均显示 `=`，表示补丁等价。
- `git diff --stat 9c0b7a4 1aa45f5`：只在 `deploy/k8s/app/application.yaml`、
  `deploy/k8s/app/bootstrap.yaml` 和 `docs/infrastructure.md` 三处显示主干镜像记录差异。
- 原始 rebase 前后 `git diff` 文件 SHA-256 分别为
  `00ca7d329a6a1b2189a6ca17f1c4b003526322d4b9633995cd52829ca6fc2d25` 和
  `b3589d7fbf187a174769959d9e743f331bfba53fc079a840ec555d2ef09f86cc`；差异来自主干
  镜像基线行和 diff 索引行，提交级等价性以上面的 `range-diff` 为准。
- `git diff --exit-code 9c0b7a4 -- backend/src`：无输出，最终运行源码与原 `9c0b7a4` 一致。

## 初次镜像记录（已回退）

应用与 bootstrap 清单、基础设施初次记录为已构建的 `9c0b7a4`：

`sha256:bdc5f299c6063ad5c91edca0e22448f9c96167b7d849b613150fd4899574cf76`

该镜像首次部署后因非 root 进程无法读取源码而失败，已回退；后续修复与替代镜像见下节。

## 部署后返工：9c0b7a4-r2

PR #265 合并为 `b940ba879076ad21ceae27f14d90e24cfe06ccf7`。首次部署 `9c0b7a4`
（digest `sha256:bdc5f299c6063ad5c91edca0e22448f9c96167b7d849b613150fd4899574cf76`）
失败：root 导入通过，但生产 uid 1000 导入报
`ModuleNotFoundError: No module named 'semantica_dataagent.cli'`。根因是构建上下文解包权限
带入镜像，源码目录为 root 所有、权限 `0700`，文件权限 `0600`，非 root 进程无法读取。

发现后立即回退到 `172.16.242.124:31640/dataagent-app:001d46d`：`ready_replicas=1`、
`healthz=200`、会话 Pod 数为 0，Langfuse 保持启用且不采集内容。

修复镜像 `9c0b7a4-r2` 基于原镜像构建，只增加
`RUN chmod -R a+rX /app/backend/src /app/schemas /app/skills /app/static`，未修改 Dockerfile
源码内容或应用逻辑。修复前后 237 个文件的内容摘要均为
`7e34fe231eb6013a19ef457ffb0f8e97fdeab014d174718befcf5beefe78661a`，只改变文件读取权限。
复验环境为 uid 1000、gid 1000、`cap-drop ALL`、`no-new-privileges`，CLI 导入和资源读取通过。

协调者部署记录（`nodepool-deployment-r2-result.json`）：镜像
`172.16.242.124:31640/dataagent-app:9c0b7a4-r2`，generation 33，ready 1，health 200，
`session_nodes=k8s-worker-03,k8s-worker-05`。应用与 bootstrap 清单、基础设施当前镜像已更新为
该镜像。r2 镜像 digest 为
`sha256:9fbb47bf331adcecbbcaacd1bc3f08ac4cb9a86bb29c58163e83f37694ad81fc`。

live 验收（`nodepool-live-acceptance.md`，2026-11-14，退出码 0）通过：Deployment
Available/Ready，HTTP `/healthz` 为 200，运行镜像 tag 与 digest 均匹配；e2e 创建的会话 Pod
实际调度到允许池内的 `k8s-worker-03`。MCP 返回 401 且可达、模型网关返回 200，外网、Neo4j、
Kubernetes API 与 registry 宿主机端口均为 `CLOSED`；`iptables -F` 因无 `NET_ADMIN` 被拒绝。
验收最后精确删除唯一测试 Pod，并确认 `NotFound`。未执行知识库写入、SQL、服务启停或端口
31601–31603 访问。

## 验证记录

### f2adfe3 返工复验（原基线 001d46d）

- `pytest tests/test_k8s_sandbox_spec.py -q -rs`：`47 passed, 2 skipped`。两个跳过项均为本机
  Docker daemon 不可用触发的容器 PID1 测试。
- `pytest tests/test_k8s_sandbox_spec.py -k 'issue_26' -v`：`13 passed, 36 deselected`。
- `pytest tests/test_k8s_sandbox_spec.py -k 'node_pool or placement or application_sessions' -q -rs`：
  `18 passed, 31 deselected`。
- `ruff check tests/e2e/checks/issue_26.py tests/test_k8s_sandbox_spec.py src/semantica_dataagent/k8s_sandbox.py`：
  `All checks passed!`。
- `ruff format --check` 同三个文件：`3 files already formatted`。
- 独立副本设置 `PYTHONPATH=$REPLICA_DIR/backend/src` 后确认导入副本源码；运行同一 spec：
  `47 passed, 2 skipped`。副本已清理。

### Red check 与 Break check

- 实现/清单回退补丁 SHA-256：
  `e9be9b7f932858f22a9113cd48562255e1fb2adcd0b0a3990b1e472459403ad5`。
- 在独立副本反向应用回退补丁后运行目标 spec：`18 failed, 29 passed, 2 skipped`。失败覆盖池配置
  仍生成 `nodeSelector`、空/非法池未抛 `ValueError`、生产清单缺少节点池 env、e2e 读取池失败，
  以及 e2e 冲突守卫被触发。恢复补丁后 `47 passed, 2 skipped`。
- e2e 冲突变异：临时恢复 `_apply_session_placement` 的无条件应用节点 `nodeSelector`，运行
  `-k 'issue_26'`：`4 failed, 9 passed, 36 deselected`。失败覆盖池配置写 `nodeSelector`、
  未拒绝冲突 `nodeSelector`、未拒绝缺失 affinity 和不匹配 affinity；恢复后 `13 passed`。
- Break check 覆盖冲突守卫、`valueFrom`/非字符串、无池旧行为、重复 env、缺少 `dataagent`
  容器；每类变异均使对应测试真实变红，恢复后全部通过。

### 初次镜像集成收尾复验

- Rebase 后及镜像记录更新后，运行 `pytest tests/test_k8s_sandbox_spec.py -q -rs`：
  `47 passed, 2 skipped`；跳过原因仍为 Docker daemon 不可用。
- `ruff check` 与 `ruff format --check` 覆盖相关源码和测试文件，均通过。
- YAML 解析 `application.yaml`、`bootstrap.yaml` 与 `kustomization.yaml` 成功；应用与
  bootstrap 的镜像均为 `172.16.242.124:31640/dataagent-app:9c0b7a4`。
- `git diff --check`：通过。

## 未运行项

- 全量回归：任务书禁止，未运行。
- 本工单本地实施未执行集群部署；r2 部署与 `backend/tests/e2e/checks/issue_26.py` 真实
  `check_session_egress` 验收由协调者执行并通过，结果见 `nodepool-live-acceptance.md`。
- 知识库写入、构建、SQL、服务启停和测试存储端口访问：任务书禁止，验收记录中亦未执行。
- 本机无 Docker daemon，涉及容器 PID1 的两个测试由 pytest 安全跳过。

## 交接证据

开发交接与独立测试交接的原始文件保留在协调者 scratch 的本票交接目录中；新增测试交接原件
归档到本票 scratch 的 `handoffs/`，不进入仓库。本文件是仓库内可保留的验证归档，不依赖临时
绝对路径或任何凭据。
