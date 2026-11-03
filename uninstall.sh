#!/usr/bin/env bash
set -euo pipefail

purge=false
case ${1:-} in
  "") ;;
  --purge) purge=true ;;
  *) echo "用法：$0 [--purge]" >&2; exit 2 ;;
esac

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
user_home=${HOME:?HOME 未设置}
data_home=${XDG_DATA_HOME:-"$user_home/.local/share"}
state_home=${XDG_STATE_HOME:-"$user_home/.local/state"}
codex_config_home=${CODEX_HOME:-"$user_home/.codex"}
install_dir="$data_home/codex-agent-monitor"
state_dir="$state_home/codex-agent-monitor"
wrapper="$user_home/.local/bin/codex-agents"
hook_config="$codex_config_home/hooks.json"

for target_path in "$install_dir" "$state_dir" "$wrapper" "$hook_config"; do
  case "$target_path" in
    /*) ;;
    *) echo "错误：卸载路径必须是绝对路径：$target_path" >&2; exit 1 ;;
  esac
done
if [[ $(basename -- "$install_dir") != codex-agent-monitor || $(basename -- "$state_dir") != codex-agent-monitor ]]; then
  echo "错误：拒绝处理异常目录。" >&2
  exit 1
fi

configurer=
if [[ -f "$script_dir/scripts/configure_hooks.py" ]]; then
  configurer="$script_dir/scripts/configure_hooks.py"
elif [[ -f "$install_dir/configure_hooks.py" ]]; then
  configurer="$install_dir/configure_hooks.py"
fi
if [[ -n $configurer && -f $hook_config ]]; then
  python3 "$configurer" remove --config "$hook_config" --hook "$install_dir/hook.py"
fi

if [[ -f $wrapper ]] && grep -q '^# Installed by codex-agent-monitor$' "$wrapper"; then
  rm -f -- "$wrapper"
  echo "已移除命令：$wrapper"
fi

if [[ -d $install_dir ]]; then
  find "$install_dir" -depth -delete
  echo "已移除程序：$install_dir"
fi

if $purge && [[ -d $state_dir ]]; then
  find "$state_dir" -depth -delete
  echo "已移除状态数据：$state_dir"
elif [[ -d $state_dir ]]; then
  echo "已保留状态数据：$state_dir（使用 --purge 可一并移除）"
fi

echo "Codex Agent Monitor 已卸载。"
