#!/usr/bin/env bash
#
# 把仓库里的代码部署到 launchd 的运行目录。
# 顺序固定：跑测试 → 拷文件 → kickstart → 轮询 /health 直到版本对上且换了进程。
# 任何一步不过就停下并非零退出；测试不过就一个文件都不拷。
#
# 运行目录默认 ~/Library/Application Support/AgentSignals，
# 可用 AGENT_SIGNALS_DEPLOY_DIR 覆盖（改端口用 AGENT_SIGNALS_PORT）。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${AGENT_SIGNALS_DEPLOY_DIR:-$HOME/Library/Application Support/AgentSignals}"
LABEL="com.edy.agent-signals"
PORT="${AGENT_SIGNALS_PORT:-8812}"
# 与 launchd plist 里跑的是同一个解释器：测试必须在它上面绿。
PYTHON="/usr/bin/python3"
HEALTH_TIMEOUT_S=20
ERR_LOG="$DEPLOY_DIR/agent-signals.err.log"

# 运行时数据，部署一律不碰：历史库、价格表、确认状态、运行时档案。
PROTECTED=(
  "agent-history.db"
  "codex_prices.json"
  ".agent-signals-state.json"
  "runtime-profiles.json"
)

die() {
  printf '部署失败：%s\n' "$1" >&2
  exit 1
}

is_protected() {
  local name="$1" guarded
  for guarded in "${PROTECTED[@]}"; do
    if [[ "$name" == "$guarded" ]]; then
      return 0
    fi
  done
  return 1
}

# 一次 curl 取回 "<version> <pid>"；服务不可达时是空串。
read_health() {
  curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null \
    | "$PYTHON" -c \
      'import json,sys; d=json.load(sys.stdin); print(d.get("version",""), d.get("pid",""))' \
      2>/dev/null || true
}

install_file() {
  local src="$1" dest_dir="$2" name
  name="$(basename "$src")"
  if is_protected "$name"; then
    printf '  跳过受保护的运行时数据：%s\n' "$name"
    return 0
  fi
  mkdir -p "$dest_dir"
  cp "$src" "$dest_dir/$name"
  printf '  → %s\n' "$dest_dir/$name"
}

cd "$REPO_DIR"

if [[ "$(cd "$DEPLOY_DIR" 2>/dev/null && pwd)" == "$REPO_DIR" ]]; then
  die "运行目录和仓库是同一个目录（$REPO_DIR），没有可部署的动作"
fi

APP_VERSION="$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' server.py | head -n 1)"
[[ -n "$APP_VERSION" ]] || die "server.py 里读不到 APP_VERSION"

printf '[1/4] 跑测试（%s）\n' "$PYTHON"
"$PYTHON" -m unittest discover tests || die "测试没过，一个文件都没拷"

printf '[2/4] 拷文件到 %s\n' "$DEPLOY_DIR"
install_file "server.py" "$DEPLOY_DIR"
# 后续阶段才会出现的模块：有才拷。
for optional in discovery.py cloud.py; do
  if [[ -f "$optional" ]]; then
    install_file "$optional" "$DEPLOY_DIR"
  fi
done
for asset in static/*; do
  if [[ -f "$asset" ]]; then
    install_file "$asset" "$DEPLOY_DIR/static"
  fi
done

# APP_VERSION 是同一阶段内不变的手写常量，光比版本号认不出「服务压根没重启」：
# 端口被别的野进程占着时，老进程会用一模一样的版本号把健康检查骗过去。所以
# kickstart 之前先记下当前的 pid，之后要求版本对上**并且**换了进程。
old_pid="$(read_health | awk '{print $2}')"
if [[ -n "$old_pid" ]]; then
  printf '当前在跑的 pid：%s\n' "$old_pid"
else
  printf '当前没有进程应答 /health（首次部署，或服务已停）\n'
fi

printf '[3/4] 重启 %s\n' "$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL" || die "launchctl kickstart 没成功"

printf '[4/4] 等 /health 报出 %s 且换掉 pid %s（最多 %ss）\n' \
  "$APP_VERSION" "${old_pid:-（无）}" "$HEALTH_TIMEOUT_S"
reported_version=""
reported_pid=""
deadline=$(( SECONDS + HEALTH_TIMEOUT_S ))
while (( SECONDS < deadline )); do
  health="$(read_health)"
  reported_version="$(printf '%s' "$health" | awk '{print $1}')"
  reported_pid="$(printf '%s' "$health" | awk '{print $2}')"
  if [[ "$reported_version" == "$APP_VERSION" && -n "$reported_pid" \
        && "$reported_pid" != "$old_pid" ]]; then
    printf '部署完成：%s 正在 %s 端口跑 %s（pid %s）\n' \
      "$LABEL" "$PORT" "$APP_VERSION" "$reported_pid"
    exit 0
  fi
  sleep 0.5
done

if [[ "$reported_version" == "$APP_VERSION" && "$reported_pid" == "$old_pid" \
      && -n "$old_pid" ]]; then
  printf '超时：%ss 内 pid 一直是 %s，服务没有被换掉——端口很可能被别的进程占着，新进程起不来\n' \
    "$HEALTH_TIMEOUT_S" "$old_pid" >&2
else
  printf '超时：%ss 内 /health 没报出 %s（最后读到版本 "%s" / pid "%s"）\n' \
    "$HEALTH_TIMEOUT_S" "$APP_VERSION" "$reported_version" "$reported_pid" >&2
fi
if [[ -f "$ERR_LOG" ]]; then
  printf -- '--- %s 末尾 40 行 ---\n' "$ERR_LOG" >&2
  tail -n 40 "$ERR_LOG" >&2
fi
exit 1
