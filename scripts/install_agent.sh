#!/usr/bin/env bash
#
# HIL2.0 集群采集 agent 一键安装（在被监控的集群上执行）
#
# 特性：
#   · 幂等：重复执行安全，已有配置会先备份再更新
#   · 默认灰度：装完是 DRY-RUN，只写本地日志，不发任何消息
#   · 只读：安装的 agent 对被监控环境严格只读
#   · 自检：装完自动跑一次采集并把结果打印出来
#
# 用法：
#   sudo ./install_agent.sh --cluster 52 --coordinator http://10.0.0.5:8848/api/ingest \
#                           --token <INGEST_TOKEN>
#
#   装完确认无误后再关灰度：
#   sudo ./install_agent.sh --cluster 52 --go-live
#
set -euo pipefail

# ---------- 默认值 ----------
CLUSTER=""
COORDINATOR=""
TOKEN=""
WEBHOOK=""
BOT_OPEN_ID=""
DATA_DIR="/data1"
SAMPLE_SECONDS="300"
INSTALL_DIR="/opt/hil-agent"
STATE_DIR="/var/lib/hil-monitor"
LOG_DIR="/var/log/hil-monitor"
RUN_DIR="/run/hil-monitor"
SERVICE_USER="hil-agent"
USER_EXPLICIT="no"
GO_LIVE="no"
SKIP_PROBE="no"
NO_DAEMON="no"
PYTHON_BIN=""

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[36m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
step() { printf '\n%s▸ %s%s\n' "$BLUE" "$*" "$OFF"; }

# 把值安全地单引号化：正则里的 | ( ) $ 和 URL 里的 & ? 都会破坏 source
sq() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

usage() {
  cat <<'USAGE'
用法：sudo ./install_agent.sh [选项]

必填（首次安装）：
  --cluster <编号>         集群编号，例如 52

投递通道（首次安装至少配置一个）：
  --coordinator <URL>      协调服务上报地址，例如 http://10.0.0.5:8848/api/ingest
  --webhook <URL>          飞书监控群 Webhook；GPU 服务器与内网隔离时可单独使用

可选：
  --token <TOKEN>          与协调服务 INGEST_TOKEN 一致的共享密钥
  --bot-open-id <ou_xxx>   业务机器人 open_id，用于监控群 @ 它
  --data-dir <路径>        数据产出目录，默认 /data1
  --sample-seconds <秒>    采集间隔，默认 300
  --install-dir <路径>     安装目录，默认 /opt/hil-agent
  --state-dir <路径>       状态目录，默认 /var/lib/hil-monitor
  --log-dir <路径>         日志目录，默认 /var/log/hil-monitor
  --run-dir <路径>         运行时目录，默认 /run/hil-monitor
  --user <用户名>          服务账号，默认 hil-agent
  --go-live                关闭灰度，开始真正外发消息
  --skip-probe             跳过安装前的现场探测
  --no-daemon              只装文件和配置，不碰 systemd / launchd
  -h, --help               显示本帮助

安装后默认处于灰度（DRY-RUN）：只写 /var/log/hil-monitor/events.jsonl，
不会给飞书发任何消息。对照人工使用记录校准 2～3 天后，再用 --go-live 开启。
USAGE
}

# ---------- 解析参数 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cluster)         CLUSTER="$2"; shift 2 ;;
    --coordinator)     COORDINATOR="$2"; shift 2 ;;
    --token)           TOKEN="$2"; shift 2 ;;
    --webhook)         WEBHOOK="$2"; shift 2 ;;
    --bot-open-id)     BOT_OPEN_ID="$2"; shift 2 ;;
    --data-dir)        DATA_DIR="$2"; shift 2 ;;
    --sample-seconds)  SAMPLE_SECONDS="$2"; shift 2 ;;
    --install-dir)     INSTALL_DIR="$2"; shift 2 ;;
    --state-dir)       STATE_DIR="$2"; shift 2 ;;
    --log-dir)         LOG_DIR="$2"; shift 2 ;;
    --run-dir)         RUN_DIR="$2"; shift 2 ;;
    --user)            SERVICE_USER="$2"; USER_EXPLICIT="yes"; shift 2 ;;
    --go-live)         GO_LIVE="yes"; shift ;;
    --skip-probe)      SKIP_PROBE="yes"; shift ;;
    --no-daemon)       NO_DAEMON="yes"; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) die "不认识的参数：$1（用 --help 看用法）" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "需要 root 权限，请用 sudo 执行"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENT_SRC="$SCRIPT_DIR/../agent/hil_agent.py"
PROBE_SRC="$SCRIPT_DIR/../agent/probe_site.py"
[[ -f "$AGENT_SRC" ]] || die "找不到 ${AGENT_SRC}，请在项目根目录下运行本脚本"

ENV_FILE="$INSTALL_DIR/.env"

# 仅更新模式（如 --go-live）：允许省略必填项
if [[ ! -f "$ENV_FILE" ]]; then
  [[ -n "$CLUSTER" ]] || die "首次安装必须指定 --cluster"
  [[ -n "$COORDINATOR" || -n "$WEBHOOK" ]] || \
    die "必须至少提供 --coordinator 或 --webhook 之一，否则事件无处可送"
fi

# ---------- 1. 环境检查 ----------
step "1/7 环境检查"

for candidate in python3 python3.11 python3.10 python3.9 python3.8; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$(command -v "$candidate")"; break; fi
done
[[ -n "$PYTHON_BIN" ]] || die "没找到 python3。agent 零依赖，但需要 Python 3.8+"
PY_VER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' \
  || die "Python 版本过低（${PY_VER}），需要 3.8+"
ok "Python ${PY_VER}（${PYTHON_BIN}）"

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  INIT_SYSTEM="systemd"
elif [[ "$(uname -s)" == "Darwin" ]]; then
  INIT_SYSTEM="launchd"
else
  INIT_SYSTEM="none"
  warn "既没有 systemd 也不是 macOS，装完需要你自己接管进程守护"
fi
ok "进程守护：$INIT_SYSTEM"

if command -v docker >/dev/null 2>&1; then
  if docker ps -a --format '{{.Names}}' >/dev/null 2>&1; then
    ok "docker 可读（当前用户）"
  else
    warn "当前用户读不到 docker。装完需把 $SERVICE_USER 加进 docker 组，否则状态会一直是「采集异常」"
  fi
else
  warn "没有 docker CLI。若本机确实不跑容器，说明这台不该装 agent"
fi

[[ -d "$DATA_DIR" ]] && ok "数据目录 $DATA_DIR 存在" || warn "数据目录 $DATA_DIR 不存在，采集会报「采集异常」"

# ---------- 2. 现场探测 ----------
if [[ "$SKIP_PROBE" == "no" && -f "$PROBE_SRC" ]]; then
  step "2/7 现场只读探测"
  say "${DIM}只执行读命令，不会改变任何东西${OFF}"
  "$PYTHON_BIN" "$PROBE_SRC" --data-dir "$DATA_DIR" 2>&1 | sed -n '/建议配置/,$p' || true
  say "${YELLOW}↑ 请核对容器名。若有「未匹配」告警，装完务必修改 $ENV_FILE 里的 HIL_*_PATTERN${OFF}"
else
  step "2/7 现场探测（已跳过）"
fi

# ---------- 3. 服务账号 ----------
step "3/7 服务账号与目录"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  ok "账号 $SERVICE_USER 已存在"
else
  if [[ "$USER_EXPLICIT" == "yes" ]]; then
    die "指定的账号 $SERVICE_USER 不存在，请先创建或换一个"
  elif [[ "$INIT_SYSTEM" == "launchd" ]]; then
    SERVICE_USER="$(stat -f '%Su' /dev/console)"
    warn "macOS 下不新建系统账号，改用当前登录用户 $SERVICE_USER"
  else
    useradd --system --create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "已创建账号 $SERVICE_USER"
  fi
fi

if getent group docker >/dev/null 2>&1; then
  if id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx docker; then
    ok "$SERVICE_USER 已在 docker 组"
  else
    usermod -aG docker "$SERVICE_USER" && ok "已把 $SERVICE_USER 加入 docker 组（只读用途）"
  fi
fi

install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$INSTALL_DIR"
install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$LOG_DIR"
install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$RUN_DIR" 2>/dev/null || true
ok "目录就绪"

# ---------- 4. 安装脚本 ----------
step "4/7 安装采集脚本"
install -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$AGENT_SRC" "$INSTALL_DIR/hil_agent.py"
[[ -f "$PROBE_SRC" ]] && install -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$PROBE_SRC" "$INSTALL_DIR/probe_site.py"
ok "已安装到 $INSTALL_DIR"

# ---------- 5. 配置 ----------
step "5/7 写入配置"
if [[ -f "$ENV_FILE" ]]; then
  BACKUP="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$ENV_FILE" "$BACKUP"
  ok "已备份原配置到 $BACKUP"
  # 复用已有值，命令行参数优先
  # 不 source：配置里含正则和 URL，交给 shell 解释既危险又易错。
  # 只按 KEY=VALUE 逐行取值，并剥掉外层引号。
  read_env() {
    local key="$1" line
    line="$(grep -m1 "^${key}=" "$BACKUP" 2>/dev/null || true)"
    line="${line#*=}"
    line="${line%\'}"; line="${line#\'}"
    line="${line%\"}"; line="${line#\"}"
    printf '%s' "$line"
  }
  HIL_MACHINE_NAME="$(read_env HIL_MACHINE_NAME)"
  HIL_COORDINATOR_URL="$(read_env HIL_COORDINATOR_URL)"
  HIL_INGEST_TOKEN="$(read_env HIL_INGEST_TOKEN)"
  FEISHU_WEBHOOK="$(read_env FEISHU_WEBHOOK)"
  HIL_CONSUMER_BOT_OPEN_ID="$(read_env HIL_CONSUMER_BOT_OPEN_ID)"
  HIL_DATA_DIR="$(read_env HIL_DATA_DIR)"
  HIL_SIM_PATTERN="$(read_env HIL_SIM_PATTERN)"
  HIL_PROXY_PATTERN="$(read_env HIL_PROXY_PATTERN)"
  HIL_TESTER_PATTERN="$(read_env HIL_TESTER_PATTERN)"
  HIL_EXTRA_TEST_PROCESSES="$(read_env HIL_EXTRA_TEST_PROCESSES)"
  CLUSTER="${CLUSTER:-${HIL_MACHINE_NAME:-}}"
  COORDINATOR="${COORDINATOR:-${HIL_COORDINATOR_URL:-}}"
  TOKEN="${TOKEN:-${HIL_INGEST_TOKEN:-}}"
  WEBHOOK="${WEBHOOK:-${FEISHU_WEBHOOK:-}}"
  BOT_OPEN_ID="${BOT_OPEN_ID:-${HIL_CONSUMER_BOT_OPEN_ID:-}}"
  DATA_DIR="${HIL_DATA_DIR:-$DATA_DIR}"
  SIM_PATTERN="${HIL_SIM_PATTERN:-}"
  PROXY_PATTERN="${HIL_PROXY_PATTERN:-}"
  TESTER_PATTERN="${HIL_TESTER_PATTERN:-}"
  EXTRA_TESTS="${HIL_EXTRA_TEST_PROCESSES:-}"
fi

DRY_LINE="HIL_DRY_RUN=1"
[[ "$GO_LIVE" == "yes" ]] && DRY_LINE="# HIL_DRY_RUN=1   # 已上线，如需回到灰度取消本行注释"

umask 077
cat > "$ENV_FILE" <<ENVEOF
# HIL2.0 集群采集配置 · 由 install_agent.sh 生成于 $(date '+%Y-%m-%d %H:%M:%S')
# agent 对被监控环境严格只读：不停容器、不杀进程、不删文件。

HIL_MACHINE_NAME=$(sq "$CLUSTER")
HIL_DATA_DIR=$(sq "$DATA_DIR")

# 投递通道（两条都配最稳，任意一条可用即可工作）
HIL_COORDINATOR_URL=$(sq "$COORDINATOR")
HIL_INGEST_TOKEN=$(sq "$TOKEN")
FEISHU_WEBHOOK=$(sq "$WEBHOOK")
HIL_CONSUMER_BOT_OPEN_ID=$(sq "$BOT_OPEN_ID")

# 采集节奏
HIL_SAMPLE_SECONDS=$SAMPLE_SECONDS
HIL_IDLE_MINUTES=15
HIL_CONFIRM_SAMPLES=3
HIL_HEARTBEAT_SAMPLES=12

# 容器识别：现场确认后建议改成精确容器名，例如
#   HIL_SIM_PATTERN='^(hil-sim-1|hil-sim-2)\$'
HIL_SIM_PATTERN=$(sq "${SIM_PATTERN:-(^|[-_])sim(ulator)?(\$|[-_])}")
HIL_PROXY_PATTERN=$(sq "${PROXY_PATTERN:-(^|[-_])proxy(\$|[-_])}")
HIL_TESTER_PATTERN=$(sq "${TESTER_PATTERN:-tester|autotest|simlib|logsim}")

# pytest 之外的真实测试进程，逗号分隔，现场确认后再填
HIL_EXTRA_TEST_PROCESSES=$(sq "${EXTRA_TESTS:-}")

# 文件路径
HIL_CLAIM_FILE=$(sq "$RUN_DIR/claim.json")
HIL_STATE_FILE=$(sq "$STATE_DIR/state.json")
HIL_LOG_FILE=$(sq "$LOG_DIR/events.jsonl")

# 灰度开关：置 1 时只写本地日志，不发任何消息
$DRY_LINE
ENVEOF
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE" 2>/dev/null || chown "$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "配置已写入 ${ENV_FILE}（权限 600）"
[[ "$GO_LIVE" == "yes" ]] && warn "已关闭灰度：agent 将真正对外发消息" || ok "当前为灰度模式，只写本地日志"

# ---------- 6. 进程守护 ----------
step "6/7 配置进程守护"
[[ "$NO_DAEMON" == "yes" ]] && INIT_SYSTEM="none"
case "$INIT_SYSTEM" in
  systemd)
    cat > /etc/systemd/system/hil-agent.service <<UNITEOF
[Unit]
Description=HIL2.0 集群只读采集
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_BIN $INSTALL_DIR/hil_agent.py
Restart=always
RestartSec=15

StateDirectory=hil-monitor
LogsDirectory=hil-monitor
RuntimeDirectory=hil-monitor

# 采集脚本本身只读，这里再从系统层面兜一道
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ReadWritePaths=$STATE_DIR $LOG_DIR $RUN_DIR
RestrictNamespaces=yes
LockPersonality=yes

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hil-agent

[Install]
WantedBy=multi-user.target
UNITEOF
    # /data1 只读挂载：脚本被改坏也删不掉数据
    [[ -d "$DATA_DIR" ]] && sed -i "/^RestrictNamespaces/i ReadOnlyPaths=$DATA_DIR" \
      /etc/systemd/system/hil-agent.service

    cat > /etc/logrotate.d/hil-agent <<ROTEOF
$LOG_DIR/events.jsonl {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
ROTEOF
    systemctl daemon-reload
    systemctl enable hil-agent >/dev/null 2>&1
    systemctl restart hil-agent
    sleep 2
    if systemctl is-active --quiet hil-agent; then
      ok "hil-agent 已启动并设为开机自启"
    else
      warn "服务未能启动，看日志：journalctl -u hil-agent -n 50"
    fi
    ;;
  launchd)
    PLIST="/Library/LaunchDaemons/com.hil2.agent.plist"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.hil2.agent</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON_BIN</string><string>$INSTALL_DIR/hil_agent.py</string></array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>UserName</key><string>$SERVICE_USER</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/agent.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/agent.err.log</string>
</dict>
</plist>
PLISTEOF
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load "$PLIST"
    ok "launchd 已加载 com.hil2.agent"
    warn "launchd 不读 .env，如需自定义变量请写进 plist 的 EnvironmentVariables"
    ;;
  *)
    warn "跳过进程守护配置。手动运行：
    sudo -u $SERVICE_USER env \$(grep -v '^#' $ENV_FILE | xargs) $PYTHON_BIN $INSTALL_DIR/hil_agent.py"
    ;;
esac

# ---------- 7. 自检 ----------
step "7/7 安装自检"
say "${DIM}跑一次采集，看看当前判定结果${OFF}"
set +e
SELFTEST="$(sudo -u "$SERVICE_USER" env \
  HIL_MACHINE_NAME="$CLUSTER" HIL_DATA_DIR="$DATA_DIR" \
  HIL_SIM_PATTERN="${SIM_PATTERN:-(^|[-_])sim(ulator)?($|[-_])}" \
  HIL_PROXY_PATTERN="${PROXY_PATTERN:-(^|[-_])proxy($|[-_])}" \
  HIL_TESTER_PATTERN="${TESTER_PATTERN:-tester|autotest|simlib|logsim}" \
  HIL_STATE_FILE="$STATE_DIR/state.json" HIL_LOG_FILE="$LOG_DIR/events.jsonl" \
  HIL_CLAIM_FILE="$RUN_DIR/claim.json" \
  HIL_DRY_RUN=1 "$PYTHON_BIN" "$INSTALL_DIR/hil_agent.py" --once --print-event 2>&1)"
set -e
say "$SELFTEST" | grep -E '集群：|依据：' || say "$SELFTEST" | tail -5

if say "$SELFTEST" | grep -q '采集异常'; then
  warn "当前判定为「采集异常」。这是安全的默认值（绝不会误判成空闲），
    但说明采集还没打通。常见原因：
      · $SERVICE_USER 读不到 docker  → 重新登录使 docker 组生效，或配 sudoers 规则
      · 容器名正则和现场不符          → 改 $ENV_FILE 里的 HIL_*_PATTERN
      · $DATA_DIR 不可读              → 检查目录权限"
else
  ok "采集正常"
fi

step "完成"
cat <<DONE
  集群编号      $CLUSTER
  安装目录      $INSTALL_DIR
  配置文件      $ENV_FILE
  本地事件日志  $LOG_DIR/events.jsonl
  灰度状态      $([[ "$GO_LIVE" == "yes" ]] && echo "已上线（会发消息）" || echo "灰度中（只写本地日志）")

  看实时日志    journalctl -u hil-agent -f
  看采集结果    tail -f $LOG_DIR/events.jsonl
  改配置        sudo vi $ENV_FILE && sudo systemctl restart hil-agent
  上线          sudo $0 --cluster $CLUSTER --go-live
DONE
