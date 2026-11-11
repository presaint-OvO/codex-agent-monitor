# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## [1.1.3] - 2026-11-11

- 子 Agent 主名称改为从初始任务提炼，Codex 自动分配的 nickname 仅在详情页作为补充信息显示。
- 为扫描窗口之外的历史子 Agent 建立轻量身份与终态索引，旧 session 不再因 `--history-hours` 被整体隐藏。
- 历史索引复用 Hooks 中已有的执行步骤，并从 rollout 恢复任务提示词、真实名称与完成/中断状态。
- 历史回放保持有界：只读取子 Agent 文件前缀和最多 256 KiB 的尾部，不完整解析陈旧 rollout。

## [1.1.2] - 2026-11-11

- 兼容 Codex CLI 0.167.1 的 multi-agent v1 rollout 格式。
- 从 `CollabAgentToolCall` 恢复子 Agent 的线程身份、nickname、创建提示词和终态。
- 在 `agent_path` 缺失时使用子线程 ID 关联执行步骤与详情。
- 修复新格式下子 Agent 完成可能误结束同 session 其他 Agent 的问题。
- 历史回放不会用较早的活动事件重新打开已结束 Agent。

## [1.1.1] - 2026-11-03

- 修复短任务名中断时产生一条“运行中”幽灵副本的问题。
- 识别 rollout 中的 `interrupted`、`cancelled` 和 `failed` 子 Agent 生命周期事件。
- 同一终态被重复上报时保留最早的真实结束时间，避免持续时间被刷新拉长。
- 短任务名只在同一 session 内唯一匹配已有子 Agent，防止跨 session 或同名误归组。

## [1.1.0] - 2026-11-03

- 在 Agent 列表中增加 session 相对时间戳。
- 以本 session 最早真实子 Agent 的开始时间为 `0m 00s`。
- 已完成 Agent 的时间戳冻结在完成时刻，运行中 Agent 实时递增。
- 隐藏记录不会改变时间轴零点，`default` 主 Agent 不参与零点计算。

## [1.0.0] - 2026-11-03

- 首个公开版本。
- 提供 session、子 Agent 与详情三级交互式终端界面。
- 展示创建提示词、执行步骤、实时状态、步骤耗时和 Agent 总耗时。
- 支持 Codex Hooks、rollout 回退扫描与外部任务归组。
- 支持隐藏、清空和恢复记录。
- 提供无第三方依赖的用户级安装、升级与卸载脚本。
