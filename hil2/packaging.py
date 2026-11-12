"""生成每台集群专属的 agent 安装包。

管理员在后台点「下载」，拿到一个 tar.gz，拷到集群上解开执行 install.sh 即可。
包里的 .env 已经按后台配置填好——不用手抄集群编号、上报地址和密钥。

密钥会随包分发，因此：
- 只有管理员能下载。
- 包里的 .env 权限设为 600。
- 下载动作写进后台审计。
"""

from __future__ import annotations

import io
import shlex
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .registry import ClusterConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def render_env(config: ClusterConfig, *, coordinator_url: str, ingest_token: str,
               bot_open_id: str = "") -> str:
    """生成 .env。值一律单引号化，正则里的 | 和 URL 里的 & 才不会破坏 source。"""
    env = config.agent_env(
        coordinator_url=coordinator_url, ingest_token=ingest_token, bot_open_id=bot_open_id
    )
    lines = [
        f"# HIL2.0 集群 {config.cluster} 采集配置",
        f"# 由管理后台生成于 {datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M:%S}",
        "#",
        "# 采集脚本对被监控环境严格只读：",
        "#   docker ps -a · docker exec <tester> pgrep -af pytest",
        "#   find <数据目录> -mmin · nvidia-smi · who",
        "# 不停容器、不杀进程、不删文件、不释放集群。",
        "",
    ]
    for key, value in env.items():
        lines.append(f"{key}={shlex.quote(value)}")
    lines += [
        "",
        "# 文件路径",
        "HIL_CLAIM_FILE=/run/hil-monitor/claim.json",
        "HIL_STATE_FILE=/var/lib/hil-monitor/state.json",
        "HIL_LOG_FILE=/var/log/hil-monitor/events.jsonl",
        "",
        "# 灰度开关：置 1 时只写本地日志，不发任何消息。",
        "# 建议先灰度跑 2～3 天，对照人工使用记录校准容器名和阈值。",
        "HIL_DRY_RUN=1",
        "",
    ]
    return "\n".join(lines)


def render_readme(config: ClusterConfig, *, coordinator_url: str) -> str:
    return f"""HIL2.0 集群采集 · {config.cluster}
{'=' * 44}

这个包是管理后台为 {config.cluster} 集群生成的，配置已经填好，
不需要再手抄集群编号、Webhook 或密钥。

安装
----
    tar xzf hil-agent-{config.cluster}.tar.gz
    cd hil-agent-{config.cluster}
    sudo ./install.sh

装完默认是**灰度模式**：只往 /var/log/hil-monitor/events.jsonl 写日志，
不会给飞书发任何消息。

校准
----
让它安静跑 2～3 天，对照大家实际的使用记录看判定准不准：

    tail -f /var/log/hil-monitor/events.jsonl

如果发现有人在用却判成「疑似空闲」，说明还有 pytest 之外的测试进程，
在管理后台把进程名补进「其他测试进程」，重新下载安装包即可。

上线
----
    sudo ./install.sh --go-live

装之前想先看看现场情况
----------------------
    python3 probe_site.py

这个工具严格只读，会列出所有容器、tester 容器里的进程、数据目录写入情况，
并给出建议的容器匹配规则。

当前配置
--------
  集群编号      {config.cluster}
  名称          {config.name or '（未命名）'}
  数据目录      {config.data_dir}
  采集间隔      {config.sample_seconds} 秒
  无活动窗口    {config.idle_minutes} 分钟
  集群直连地址  {coordinator_url or '未配置（符合内网隔离部署）'}
  监控群 webhook {'已配置（主通道）' if config.webhook_url else '未配置（只走直连上报）'}

  sim 容器      {config.sim_pattern}
  proxy 容器    {config.proxy_pattern}
  tester 容器   {config.tester_pattern}
  其他测试进程  {config.extra_test_processes or '（无，只认 pytest）'}

安全边界
--------
1. 只读监控——不停容器、不杀进程、不删数据、不重启机器。
2. 采集异常绝不判空闲——读不到就报「采集异常」，只告警管理员。
3. 绝不自动释放——没人回复确认卡片也不会把机器收走。
4. 登录会话仅辅助定位——只有测试进程或近期数据活动才能确认“正在使用”。

.env 里有上报密钥，权限已设为 600，请不要提交到代码仓库或转发给无关同事。
"""


def render_install_wrapper(config: ClusterConfig) -> str:
    """包内的 install.sh：把参数固定好，管理员只要 sudo ./install.sh。"""
    return f"""#!/usr/bin/env bash
# HIL2.0 集群 {config.cluster} 采集安装
# 由管理后台生成，配置已在同目录 .env 里填好。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)"

[[ $EUID -eq 0 ]] || {{ echo "需要 root 权限，请用 sudo 执行" >&2; exit 1; }}

INSTALL_DIR="${{INSTALL_DIR:-/opt/hil-agent}}"

# 把随包带来的 .env 放到位，install_agent.sh 会读它并保留已有取值
install -d -m 755 "$INSTALL_DIR"
if [[ -f "$INSTALL_DIR/.env" ]]; then
  cp -p "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak.$(date +%Y%m%d%H%M%S)"
fi
install -m 600 "$HERE/.env" "$INSTALL_DIR/.env"

exec "$HERE/install_agent.sh" \\
  --cluster {shlex.quote(config.cluster)} \\
  --data-dir {shlex.quote(config.data_dir)} \\
  --install-dir "$INSTALL_DIR" \\
  "$@"
"""


def build_bundle(config: ClusterConfig, *, coordinator_url: str, ingest_token: str,
                 bot_open_id: str = "") -> tuple[bytes, str]:
    """打出 tar.gz。返回 (内容, 建议文件名)。"""
    root = f"hil-agent-{config.cluster}"
    buffer = io.BytesIO()
    now = int(time.time())

    def add_text(tar: tarfile.TarFile, name: str, text: str, mode: int = 0o644) -> None:
        data = text.encode("utf-8")
        info = tarfile.TarInfo(f"{root}/{name}")
        info.size = len(data)
        info.mode = mode
        info.mtime = now
        tar.addfile(info, io.BytesIO(data))

    def add_file(tar: tarfile.TarFile, source: Path, name: str, mode: int = 0o755) -> None:
        add_text(tar, name, source.read_text(encoding="utf-8"), mode)

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        add_file(tar, AGENT_DIR / "hil_agent.py", "hil_agent.py")
        add_file(tar, AGENT_DIR / "probe_site.py", "probe_site.py")
        add_file(tar, SCRIPTS_DIR / "install_agent.sh", "install_agent.sh")
        add_text(tar, "install.sh", render_install_wrapper(config), 0o755)
        # 含密钥，权限 600
        add_text(tar, ".env", render_env(
            config, coordinator_url=coordinator_url,
            ingest_token=ingest_token, bot_open_id=bot_open_id,
        ), 0o600)
        add_text(tar, "README.txt", render_readme(config, coordinator_url=coordinator_url))

    return buffer.getvalue(), f"{root}.tar.gz"


def render_oneliner(config: ClusterConfig, *, base_url: str, download_token: str) -> str:
    """给管理员复制粘贴的一行安装命令。"""
    url = f"{base_url.rstrip('/')}/api/admin/clusters/{config.cluster}/agent.tar.gz"
    return (
        f"curl -fsSL -H 'Authorization: Bearer {download_token}' {url} "
        f"| tar xz && sudo ./hil-agent-{config.cluster}/install.sh"
    )
