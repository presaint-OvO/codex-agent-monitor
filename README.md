# Codex Agent Monitor

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20WSL-4EAA25.svg)](#运行要求)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个面向 Codex CLI 的用户级交互式终端监控器。它在独立终端中集中展示 session、子 Agent、任务提示词、执行步骤、状态和耗时，不需要修改被监控的项目。

## 功能

- 主页按 Codex 中的真实标题展示 session、项目和状态，内部 ID 只作补充信息。
- 进入 session 后查看从初始任务提炼出的子 Agent 名称、任务概述和运行状态；Codex 自动分配的 nickname 只在详情页作为补充信息显示。
- 显示 Agent 从启动到结束的完整耗时；运行中显示当前已运行时长，短步骤精确到毫秒。
- 在 Agent 列表的“步数”后显示 session 相对时间戳：以本 session 最早子 Agent 的开始时间为 `0m 00s`，完成后冻结在相对完成时刻。
- 识别主 session 通过 `codex_tui.create_thread` 调度的独立任务，将它们归到创建者下面，避免与主 session 并列重复显示。
- 支持隐藏单个 Agent、清空某个 session 或全部记录，并可恢复；这些操作不会删除 Codex 原始日志。
- 优先使用 Codex 用户级 Hooks，并以只读方式扫描 rollout；近期会话读取完整步骤，更早的历史会话建立轻量身份与终态索引，因此安装前及扫描窗口外的子 Agent 仍可查看。
- 兼容 Codex CLI multi-agent v1 的线程 ID、nickname 与 `CollabAgentToolCall` 生命周期记录。
- curses 备用屏幕原地刷新，保持当前选择与滚动位置。
- 仅使用 Python 标准库，无第三方运行时依赖。

## 快速安装

```bash
git clone https://github.com/presaint-OvO/codex-agent-monitor.git
cd codex-agent-monitor
./install.sh
```

重新打开 Codex 会话，使用户级 Hook 配置生效。之后在任意目录的新终端运行：

```bash
codex-agents
```

安装脚本会：

- 将程序复制到 `${XDG_DATA_HOME:-~/.local/share}/codex-agent-monitor`；
- 在 `~/.local/bin` 安装 `codex-agents` 命令；
- 安全合并 `${CODEX_HOME:-~/.codex}/hooks.json`，保留已有 Hook。

如果 shell 找不到命令，请将下面一行加入 `~/.bashrc` 或对应 shell 配置，然后重开终端：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 运行要求

- Linux 或 WSL2
- Python 3.10 或更高版本
- Codex CLI（需产生本地 session rollout；Hooks 可获得更完整、及时的数据）
- 支持 curses 的终端

目前已在 Codex CLI 0.154.0、0.167.1、Python 3.10 和 WSL2 上验证。Codex 的内部 rollout 格式并非稳定公共 API，未来版本若改变格式，工具可能需要同步适配。

## 交互操作

界面分为 session、Agent 列表和 Agent 详情三级：

- `↑` / `↓` 或 `j` / `k`：移动选择或滚动
- `Enter`：进入选中项
- `q` / `Esc` / `←`：返回上一级；在主页退出
- `PgUp` / `PgDn`、`g` / `G`：详情页翻页、跳到开头或末尾
- `d`：隐藏当前 Agent
- `c`：隐藏当前 session 的全部 Agent
- `C`：隐藏所有 Agent
- `u`：恢复此前隐藏的记录
- `r`：立即重新扫描

后台刷新不会改变当前页面、选中项或滚动位置。

常用命令行选项：

```bash
codex-agents --demo             # 打开演示界面
codex-agents --once             # 输出一次当前状态
codex-agents --clear            # 清空 Hook 事件缓存
codex-agents --clear-agents     # 隐藏全部 Agent 后退出
codex-agents --restore-hidden   # 恢复全部隐藏记录后退出
codex-agents --history-hours 6  # 最近 6 小时读取完整步骤；更早会话仍显示轻量索引
codex-agents --stale-minutes 90 # 调整缺少终态时的兜底阈值
codex-agents --plain            # 使用普通文本刷新模式
codex-agents --native           # 打开 Codex 内置 Agent 控制中心
```

## 数据与隐私

工具会读取：

- `~/.codex/session_index.jsonl`
- `~/.codex/sessions/` 下的 rollout（近期记录完整读取，历史记录只读取身份前缀与终态尾部）
- `${XDG_STATE_HOME:-~/.local/state}/codex-agent-monitor/events.jsonl`

隐藏规则保存在同一状态目录的 `visibility.json`。这些内容可能包含任务描述、提示词、工具参数、文件路径和结果摘要，请勿把真实日志提交到公开仓库或发送给不受信任的人。

监控器不会修改 Codex session 日志，也不会尝试解密 Codex 保存的密文。创建提示词不可直接取得时，它会尝试从子 Agent 已读取的交接文件中回填。

## 更新与卸载

更新代码后，在仓库中重新运行：

```bash
git pull
./install.sh
```

卸载程序并移除它自己的 Hook（会保留其他 Hook 和监控状态）：

```bash
./uninstall.sh
```

连同监控器自己的事件与隐藏状态一起移除：

```bash
./uninstall.sh --purge
```

## 开发

```bash
make test
```

主要目录：

```text
src/       监控器与 Hook 入口
scripts/   Hook 配置合并工具
tests/     rollout、状态归并和 UI 数据模型回归测试
```

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全与隐私问题见 [SECURITY.md](SECURITY.md)。

## License

[MIT](LICENSE)
