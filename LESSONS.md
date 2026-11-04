<!-- retro-done: -->
# 教训

本项目的代码由 agent 编写、审查和合并。每个 spec 的票全部合并后，协调者复盘三个问题：哪些改完又返工了，哪些不该合进来，哪些验证做得太薄。这里只记录复盘得出、但还没有升级的教训。改代码之前先读这份文件，review 时逐条对照。

每条教训先写一句规则，再写证据和检查办法。Seen 统计这条教训在几个不同 spec 的复盘里出现过。出现在 3 个不同 spec 里的教训，会升级到 [docs/code-standards.md](docs/code-standards.md)，这里只保留一行指针。

<!--
条目格式（按主题分节，例如：## 读懂报告、## 修到同类、## 验证）：

**<一句话规则>**
- 证据：<票 / PR / commit>：<发生了什么，后果是什么>
- 修复：<commit / PR>
- 怎么提前发现：<下次用什么检查能拦住>
- Done right：<做对的例子>（可选）
- Seen 1× (spec <id> · YYYY-MM-DD)
-->

## 已升级到 code-standards

<!-- - **<规则>**（spec <a>、<b>、<c>）→ docs/code-standards.md -->
