#!/usr/bin/env bash
# 在沙箱里验证 install_agent.sh 的配置生成逻辑。
# 把需要 root 的系统调用打桩，只跑真正容易出错的部分。
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

mkdir -p "$SANDBOX/bin" "$SANDBOX/opt" "$SANDBOX/var/lib/hil-monitor" "$SANDBOX/data1"

# --- 打桩：所有会改系统的命令 ---
for cmd in useradd usermod systemctl launchctl getent id sudo chown; do
  cat > "$SANDBOX/bin/$cmd" <<'STUB'
#!/usr/bin/env bash
case "$(basename "$0")" in
  id)     if [[ "$1" == "-nG" ]]; then echo "staff docker"
          elif [[ "$1" == "definitely-not-a-user" ]]; then exit 1
          else echo "uid=501($1)"; fi ;;
  getent) exit 1 ;;
  sudo)   shift; while [[ "$1" == *=* ]]; do shift; done; exec "$@" ;;
  *)      exit 0 ;;
esac
STUB
  chmod +x "$SANDBOX/bin/$cmd"
done

# install 打桩成 cp（避免 -o/-g 需要 root）
cat > "$SANDBOX/bin/install" <<'STUB'
#!/usr/bin/env bash
args=(); dirmode=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) dirmode=1; shift ;;
    -m|-o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
if [[ -n "$dirmode" ]]; then mkdir -p "${args[@]}"; else cp "${args[@]}"; fi
STUB
chmod +x "$SANDBOX/bin/install"

PATH="$SANDBOX/bin:$PATH"
export PATH

fail=0
check() {
  if [[ "$2" == *"$3"* ]]; then
    printf '  \033[32m✓\033[0m %s\n' "$1"
  else
    printf '  \033[31m✗\033[0m %s\n     期望包含：%s\n' "$1" "$3"; fail=1
  fi
}
check_absent() {
  if [[ "$2" != *"$3"* ]]; then printf '  \033[32m✓\033[0m %s\n' "$1"
  else printf '  \033[31m✗\033[0m %s（不该出现：%s）\n' "$1" "$3"; fail=1; fi
}

echo "▸ 1. --help 可用且不需要 root"
HELP="$(bash "$ROOT_DIR/scripts/install_agent.sh" --help)"
check "帮助里说明了灰度默认" "$HELP" "DRY-RUN"
check "帮助里有 --cluster" "$HELP" "--cluster"
check "帮助说明投递通道可二选一" "$HELP" "首次安装至少配置一个"

echo "▸ 2. 非 root 执行必须干净拒绝"
set +e
NONROOT="$(bash "$ROOT_DIR/scripts/install_agent.sh" --cluster 52 --coordinator http://x 2>&1)"
rc=$?
set -e
check "退出码非 0" "$rc" "1"
check "提示需要 sudo" "$NONROOT" "sudo"

echo "▸ 3. 不认识的参数必须拒绝"
set +e
BAD="$(bash "$ROOT_DIR/scripts/install_agent.sh" --wat 2>&1)"
set -e
check "拒绝未知参数" "$BAD" "不认识的参数"

echo "▸ 4. 沙箱安装：生成的配置正确"
# 直接执行主体，绕过 root 检查
SANDBOX_SCRIPT="$SANDBOX/install.sh"
sed 's/^\[\[ \$EUID -eq 0 \]\].*$/: # root check stubbed for test/' \
    "$ROOT_DIR/scripts/install_agent.sh" > "$SANDBOX_SCRIPT"
chmod +x "$SANDBOX_SCRIPT"
mkdir -p "$SANDBOX/scripts" "$SANDBOX/agent"
cp "$ROOT_DIR/agent/hil_agent.py" "$ROOT_DIR/agent/probe_site.py" "$SANDBOX/agent/"
cp "$SANDBOX_SCRIPT" "$SANDBOX/scripts/install_agent.sh"

set +e
OUT="$(cd "$SANDBOX" && bash scripts/install_agent.sh \
  --cluster 52 \
  --coordinator http://10.0.0.5:8848/api/ingest \
  --token 'secret-token-abc' \
  --data-dir "$SANDBOX/data1" \
  --install-dir "$SANDBOX/opt/hil-agent" \
  --state-dir "$SANDBOX/var/lib/hil-monitor" \
  --log-dir "$SANDBOX/var/log/hil-monitor" \
  --run-dir "$SANDBOX/run/hil-monitor" \
  --user "$(whoami)" \
  --skip-probe --no-daemon 2>&1)"
set -e

ENV_FILE="$SANDBOX/opt/hil-agent/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  printf '  \033[31m✗\033[0m 没有生成 .env\n%s\n' "$OUT"; exit 1
fi
ENV_CONTENT="$(cat "$ENV_FILE")"
check "集群编号写入" "$ENV_CONTENT" "HIL_MACHINE_NAME='52'"
check "上报地址写入" "$ENV_CONTENT" "HIL_COORDINATOR_URL='http://10.0.0.5:8848/api/ingest'"
check "密钥写入" "$ENV_CONTENT" "HIL_INGEST_TOKEN='secret-token-abc'"
check "默认灰度开启" "$ENV_CONTENT" "HIL_DRY_RUN=1"
check_absent "灰度行未被注释掉" "$ENV_CONTENT" "# HIL_DRY_RUN=1   # 已上线"
check "容器正则有默认值" "$ENV_CONTENT" "HIL_SIM_PATTERN="
check "本地日志路径可覆盖" "$ENV_CONTENT" "HIL_LOG_FILE='$SANDBOX/var/log/hil-monitor/events.jsonl'"
check "agent 已复制" "$(ls "$SANDBOX/opt/hil-agent/")" "hil_agent.py"

PERMS="$(stat -f '%Lp' "$ENV_FILE" 2>/dev/null || stat -c '%a' "$ENV_FILE")"
check ".env 权限 600" "$PERMS" "600"

echo "▸ 4b. 首次安装允许只配置 Webhook"
set +e
WEBHOOK_ONLY_OUT="$(cd "$SANDBOX" && bash scripts/install_agent.sh \
  --cluster 58 \
  --webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/test-only' \
  --data-dir "$SANDBOX/data1" \
  --install-dir "$SANDBOX/opt/hil-agent-webhook" \
  --state-dir "$SANDBOX/var/lib/hil-monitor-webhook" \
  --log-dir "$SANDBOX/var/log/hil-monitor-webhook" \
  --run-dir "$SANDBOX/run/hil-monitor-webhook" \
  --user "$(whoami)" \
  --skip-probe --no-daemon 2>&1)"
webhook_only_rc=$?
set -e
check "Webhook 单通道安装成功" "$webhook_only_rc" "0"
WEBHOOK_ENV="$(cat "$SANDBOX/opt/hil-agent-webhook/.env")"
check "Webhook 写入配置" "$WEBHOOK_ENV" \
  "FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/test-only'"
check "协调服务可以留空" "$WEBHOOK_ENV" "HIL_COORDINATOR_URL=''"

echo "▸ 5. 重复执行幂等，且会备份旧配置"
set +e
RERUN="$(cd "$SANDBOX" && bash scripts/install_agent.sh --cluster 52 --go-live \
   --install-dir "$SANDBOX/opt/hil-agent" --user "$(whoami)" \
   --state-dir "$SANDBOX/var/lib/hil-monitor" --log-dir "$SANDBOX/var/log/hil-monitor" \
   --run-dir "$SANDBOX/run/hil-monitor" \
   --data-dir "$SANDBOX/data1" --skip-probe --no-daemon 2>&1)"
rerun_rc=$?
set -e
[[ $rerun_rc -ne 0 ]] && printf '  \033[33m第二次执行输出：\033[0m\n%s\n' "$RERUN"
ENV2="$(cat "$ENV_FILE")"
check "上线后灰度被注释" "$ENV2" "# HIL_DRY_RUN=1"
check "原有上报地址被保留" "$ENV2" "HIL_COORDINATOR_URL='http://10.0.0.5:8848/api/ingest'"
check "原有密钥被保留" "$ENV2" "HIL_INGEST_TOKEN='secret-token-abc'"
# 备份文件是 .env.bak.* ，点开头，必须用 -A 才看得到
BACKUPS="$(ls -A "$SANDBOX/opt/hil-agent/" | grep -c '\.env\.bak\.' || true)"
check "旧配置已备份" "$BACKUPS" "1"

echo "▸ 5b. 生成的 .env 必须能被 shell 安全 source（正则里的 | 曾导致语法错误）"
set +e
SOURCE_ERR="$(bash -c "set -a; . '$ENV_FILE'; set +a; echo OK" 2>&1)"
set -e
check "source 无语法错误" "$SOURCE_ERR" "OK"
check "正则值完整保留" "$(bash -c "set -a; . '$ENV_FILE'; set +a; echo \$HIL_TESTER_PATTERN")" "tester|autotest"

echo "▸ 6. 显式指定不存在的账号必须拒绝，而不是悄悄换成别人"
set +e
NOUSER="$(cd "$SANDBOX" && bash scripts/install_agent.sh --cluster 52 \
   --coordinator http://x --install-dir "$SANDBOX/opt/other" \
   --state-dir "$SANDBOX/var/lib/o" --log-dir "$SANDBOX/var/log/o" --run-dir "$SANDBOX/run/o" \
   --data-dir "$SANDBOX/data1" --user "definitely-not-a-user" --skip-probe --no-daemon 2>&1)"
set -e
check "拒绝不存在的账号" "$NOUSER" "不存在"

echo
if [[ $fail -eq 0 ]]; then
  printf '\033[32m全部通过\033[0m\n'
else
  printf '\033[31m有失败项\033[0m\n'; exit 1
fi
