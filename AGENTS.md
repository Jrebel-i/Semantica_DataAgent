# 给编码 agent 的说明

<!-- 本文件不超过 16KB（16,000 字节）。agy 单文件上限 24,000 字节、Codex 合计上限 32 KiB，超出部分会被悄悄截掉。这里只放路由表和每个任务都需要的规则；状态、进度、历史和子系统细节写进 docs/，在路由表里指过去。 -->

## 开发规范

写代码、写测试或做 review 之前，先读下面三份：

- 通用规则：`~/.claude/dev-standards/rules.md`。
- 本项目规则：[docs/code-standards.md](docs/code-standards.md)，与通用规则冲突时以它为准。
- 教训：[LESSONS.md](LESSONS.md)，记录最近合并的工作里犯过的错，以及能提前拦住它们的规则。

<!-- 新增 docs/subsystems/、GLOSSARY.md 或 docs/adr/ 时，从 ~/.claude/dev-standards/templates/AGENTS.md.tmpl 补回对应的路由小节。 -->

## Agent skills

### Issue tracker

Issue 和 spec 记在 GitHub Issues（`Jrebel-i/Semantica_DataAgent`），用 `gh` 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

使用五个默认标签：needs-triage、needs-info、ready-for-agent、ready-for-human、wontfix。见 `docs/agents/triage-labels.md`。

### Domain docs

单一上下文：根目录的 `GLOSSARY.md` 和 `docs/adr/`。见 `docs/agents/domain.md`。

## 项目特有知识

本项目基于 Semantica 搭建 DataAgent。

新增在线 agent（runner 插件）见 [docs/agents/adding-an-agent.md](docs/agents/adding-an-agent.md)。

<!-- 只写 agent 在代码和配置里查不到的内容：约定、坑、某个选择背后的原因。命令、目录结构这类一查就知道的，不要写在这里。 -->
