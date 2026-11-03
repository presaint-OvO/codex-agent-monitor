#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：需要 Python 3.10 或更高版本。" >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "错误：需要 Python 3.10 或更高版本。" >&2
  exit 1
fi

user_home=${HOME:?HOME 未设置}
data_home=${XDG_DATA_HOME:-"$user_home/.local/share"}
bin_dir="$user_home/.local/bin"
codex_config_home=${CODEX_HOME:-"$user_home/.codex"}
install_dir="$data_home/codex-agent-monitor"
hook_config="$codex_config_home/hooks.json"

for target_path in "$user_home" "$data_home" "$bin_dir" "$codex_config_home" "$install_dir"; do
  case "$target_path" in
    /*) ;;
    *) echo "错误：安装路径必须是绝对路径：$target_path" >&2; exit 1 ;;
  esac
done

mkdir -p -- "$install_dir" "$bin_dir" "$codex_config_home"
install -m 0755 -- "$repo_dir/src/monitor.py" "$install_dir/monitor.py"
install -m 0755 -- "$repo_dir/src/hook.py" "$install_dir/hook.py"
install -m 0755 -- "$repo_dir/scripts/configure_hooks.py" "$install_dir/configure_hooks.py"
install -m 0755 -- "$repo_dir/uninstall.sh" "$install_dir/uninstall.sh"
install -m 0644 -- "$repo_dir/README.md" "$install_dir/README.md"
install -m 0644 -- "$repo_dir/LICENSE" "$install_dir/LICENSE"
install -m 0644 -- "$repo_dir/VERSION" "$install_dir/VERSION"

wrapper="$bin_dir/codex-agents"
wrapper_tmp=$(mktemp "$bin_dir/.codex-agents.XXXXXX")
cleanup_wrapper() {
  if [[ -n ${wrapper_tmp:-} && -e $wrapper_tmp ]]; then
    rm -f -- "$wrapper_tmp"
  fi
}
trap cleanup_wrapper EXIT
printf '#!/usr/bin/env bash\n# Installed by codex-agent-monitor\nexec python3 %q "$@"\n' \
  "$install_dir/monitor.py" >"$wrapper_tmp"
chmod 0755 "$wrapper_tmp"
mv -f -- "$wrapper_tmp" "$wrapper"
wrapper_tmp=

python3 "$install_dir/configure_hooks.py" install \
  --config "$hook_config" \
  --hook "$install_dir/hook.py"

echo
echo "Codex Agent Monitor 已安装。"
echo "运行：codex-agents"
if [[ :$PATH: != *":$bin_dir:"* ]]; then
  echo "提示：请将 $bin_dir 加入 PATH。"
fi
echo "请重新打开 Codex 会话，让用户级 Hooks 生效。"
