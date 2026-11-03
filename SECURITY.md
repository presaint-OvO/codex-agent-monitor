# Security Policy

## 报告问题

请通过仓库的私密联系方式报告可能导致本地数据泄露、任意命令执行或破坏 Codex 配置的问题。确认问题前，请勿公开包含利用细节或真实 session 内容的 Issue。

## 数据边界

Codex Agent Monitor 是本地只读监控工具，但其界面和事件缓存可能包含任务提示词、命令、文件路径及结果摘要。请把 `${XDG_STATE_HOME:-~/.local/state}/codex-agent-monitor` 视作敏感数据，不要公开分享。

安装器只管理带有 `codex-agent-monitor/hook.py` 标记的 Hook；卸载时应保留所有无关配置。
