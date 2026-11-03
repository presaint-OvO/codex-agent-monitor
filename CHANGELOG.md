# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

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
