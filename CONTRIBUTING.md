# 参与贡献

欢迎提交 Issue 和合并请求。请尽量让改动保持小而可验证，并说明所适配的 Codex CLI、Python 和终端环境版本。

## 本地开发

项目只依赖 Python 标准库。克隆后运行：

```bash
make test
```

涉及安装器时，请使用临时 `HOME`、`CODEX_HOME`、`XDG_DATA_HOME` 和 `XDG_STATE_HOME` 做隔离测试，不要覆盖真实用户配置。

## 提交要求

- 新行为应附带回归测试。
- 保留对现有 `hooks.json` 配置的兼容，不删除其他工具的 Hook。
- session rollout 解析失败时应降级而不是影响 Codex 本身。
- Hook 必须快速返回，任何监控错误都不能中断 Agent 工作。
- 保持 Python 3.10 兼容，避免引入非必要的第三方依赖。

## 隐私

不要提交真实的 `~/.codex` session、事件日志、访问令牌、SSH 密钥、用户名、私人项目路径或完整任务提示词。测试 fixture 应使用完全虚构的数据并尽量精简。
