#!/usr/bin/env python3
"""P0 现场事实确认工具（严格只读）。

交接文件第 10 节 P0 要求在真机上先确认四件事，本脚本一次跑完并给出建议配置：

1. sim / proxy / tester 的准确容器名。
2. 除 pytest 外是否还有其他真实测试进程。
3. /data1 是否是唯一产出目录，是否需要按子目录过滤。
4. 该「集群编号」是否包含多个节点。

只执行读命令：docker ps / docker inspect / docker exec ps / ls / df / find / nvidia-smi。
不会创建、修改、删除任何东西。

用法：
    python3 probe_site.py                 # 人类可读报告
    python3 probe_site.py --json          # 机器可读，便于归档
    python3 probe_site.py --data-dir /data1 --top 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_PATTERNS = {
    "sim": r"(^|[-_])sim(ulator)?($|[-_])",
    "proxy": r"(^|[-_])proxy($|[-_])",
    "tester": r"tester|autotest|simlib|logsim",
}

# 在 tester 容器里认为「可能是测试」的进程关键词，用于发现 pytest 之外的种类。
TEST_HINTS = [
    "pytest", "py.test", "robot", "unittest", "nose", "tox",
    "canoe", "capl", "vector", "dspace", "controldesk",
    "python -m", "run_test", "autotest", "testcase",
]

TIMEOUT = 30


def run(argv: Sequence[str], timeout: int = TIMEOUT) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
        return {
            "argv": list(argv),
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "ok": completed.returncode == 0,
        }
    except FileNotFoundError:
        return {"argv": list(argv), "ok": False, "error": "命令不存在", "stdout": "", "stderr": ""}
    except subprocess.TimeoutExpired:
        return {"argv": list(argv), "ok": False, "error": "命令超时", "stdout": "", "stderr": ""}
    except Exception as exc:
        return {
            "argv": list(argv),
            "ok": False,
            "error": type(exc).__name__,
            "stdout": "",
            "stderr": "",
        }


def probe_permissions(data_dir: str) -> Dict[str, Any]:
    """确认最小权限是否到位。缺权限就必须在上线前补，否则只能一直 UNKNOWN。"""
    docker = run(("docker", "ps", "-a", "--format", "{{.Names}}"))
    return {
        "docker_cli_present": shutil.which("docker") is not None,
        "docker_readable": docker["ok"],
        "docker_error": docker.get("error") or docker.get("stderr", ""),
        "data_dir": data_dir,
        "data_dir_exists": os.path.isdir(data_dir),
        "data_dir_readable": os.access(data_dir, os.R_OK | os.X_OK),
        "nvidia_smi_present": shutil.which("nvidia-smi") is not None,
        "find_present": shutil.which("find") is not None,
        "running_as_root": os.geteuid() == 0 if hasattr(os, "geteuid") else False,
    }


def probe_containers(patterns: Dict[str, str]) -> Dict[str, Any]:
    result = run(("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Image}}\t{{.Status}}"))
    if not result["ok"]:
        return {"error": result.get("error") or result.get("stderr"), "containers": []}

    containers: List[Dict[str, str]] = []
    for line in result["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            containers.append(
                {"name": parts[0], "state": parts[1], "image": parts[2], "status": parts[3]}
            )

    compiled = {role: re.compile(pattern, re.I) for role, pattern in patterns.items()}
    for item in containers:
        item["matched_roles"] = [
            role for role, regex in compiled.items() if regex.search(item["name"])
        ]

    matched = {
        role: [item["name"] for item in containers if role in item["matched_roles"]]
        for role in patterns
    }
    unmatched = [item["name"] for item in containers if not item["matched_roles"]]
    ambiguous = [
        item["name"] for item in containers if len(item["matched_roles"]) > 1
    ]
    return {
        "containers": containers,
        "matched": matched,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "missing_roles": [role for role, names in matched.items() if not names],
    }


def probe_processes(tester_names: List[str]) -> Dict[str, Any]:
    """列出 tester 容器里的进程，人工确认哪些算「真实测试」。"""
    out: Dict[str, Any] = {}
    for name in tester_names:
        result = run(("docker", "exec", name, "ps", "-eo", "pid,etimes,args"))
        if not result["ok"]:
            out[name] = {"error": result.get("error") or result.get("stderr", "")[:200]}
            continue
        lines = result["stdout"].splitlines()[1:]
        hits: List[Dict[str, Any]] = []
        for line in lines:
            lowered = line.lower()
            matched = [hint for hint in TEST_HINTS if hint in lowered]
            if matched:
                hits.append({"line": line.strip()[:200], "hints": matched})
        out[name] = {
            "process_count": len(lines),
            "test_like": hits,
            "sample": [line.strip()[:200] for line in lines[:20]],
        }
    return out


def probe_data_dirs(data_dir: str, top: int, minutes: int) -> Dict[str, Any]:
    info: Dict[str, Any] = {"data_dir": data_dir}
    if not os.path.isdir(data_dir):
        info["error"] = "目录不存在或不可读"
        return info

    try:
        entries = sorted(os.listdir(data_dir))[:top]
    except OSError as exc:
        info["error"] = type(exc).__name__
        return info
    info["top_level"] = entries

    recent = run(
        ("find", data_dir, "-xdev", "-type", "f", "-mmin", "-{}".format(minutes), "-print")
    )
    if recent["ok"]:
        files = [line for line in recent["stdout"].splitlines() if line][:top]
        info["recent_files"] = files
        info["recent_count"] = len(files)
        # 按一级子目录聚合，判断写入是否集中在某几个任务目录。
        buckets: Dict[str, int] = {}
        for path in files:
            relative = os.path.relpath(path, data_dir)
            bucket = relative.split(os.sep)[0] if os.sep in relative else "(根目录)"
            buckets[bucket] = buckets.get(bucket, 0) + 1
        info["recent_by_subdir"] = buckets
    else:
        info["recent_error"] = recent.get("error") or recent.get("stderr", "")[:200]

    # 其他可能的产出目录，用于确认 /data1 是不是唯一。
    candidates = [path for path in ("/data", "/data2", "/data3", "/mnt/data", "/home") if os.path.isdir(path)]
    info["other_candidate_dirs"] = [path for path in candidates if path != data_dir]

    mounts = run(("df", "-h"))
    if mounts["ok"]:
        info["filesystems"] = mounts["stdout"].splitlines()[:15]
    return info


def probe_topology() -> Dict[str, Any]:
    """判断一个「集群编号」是不是包含多个节点。"""
    info: Dict[str, Any] = {"hostname": socket.gethostname()}
    swarm = run(("docker", "info", "--format", "{{.Swarm.LocalNodeState}}"))
    if swarm["ok"]:
        info["swarm_state"] = swarm["stdout"]
        if swarm["stdout"] not in ("", "inactive"):
            nodes = run(("docker", "node", "ls", "--format", "{{.Hostname}}\t{{.Status}}"))
            if nodes["ok"]:
                info["swarm_nodes"] = nodes["stdout"].splitlines()
    compose = run(
        ("docker", "ps", "-a", "--format", "{{.Names}}\t{{.Label \"com.docker.compose.project\"}}")
    )
    if compose["ok"]:
        projects = {
            line.split("\t")[1]
            for line in compose["stdout"].splitlines()
            if "\t" in line and line.split("\t")[1]
        }
        info["compose_projects"] = sorted(projects)
    return info


def probe_gpus() -> Dict[str, Any]:
    result = run(
        (
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader",
        )
    )
    if not result["ok"]:
        return {"available": False, "error": result.get("error") or result.get("stderr", "")[:200]}
    return {"available": True, "rows": result["stdout"].splitlines()}


def suggest_config(report: Dict[str, Any]) -> List[str]:
    """把探测结果翻译成可以直接贴进 .env 的配置建议。"""
    lines: List[str] = []
    containers = report.get("containers", {})
    matched = containers.get("matched", {})

    for role in ("sim", "proxy", "tester"):
        names = matched.get(role) or []
        if not names:
            lines.append(
                "# ⚠️ {} 角色没有匹配到任何容器，上线前必须确认真实容器名，否则会一直 UNKNOWN".format(role)
            )
        elif len(names) > 3:
            lines.append("# {} 匹配到 {} 个容器，建议收紧正则：{}".format(role, len(names), names[:5]))
        else:
            escaped = "|".join(re.escape(name) for name in names)
            lines.append("HIL_{}_PATTERN='^({})$'".format(role.upper(), escaped))

    extra: List[str] = []
    for payload in (report.get("processes") or {}).values():
        for hit in payload.get("test_like", []):
            for hint in hit["hints"]:
                if hint not in ("pytest", "py.test") and hint not in extra:
                    extra.append(hint)
    if extra:
        lines.append("# 现场发现 pytest 之外的疑似测试进程，确认后再启用：")
        lines.append("# HIL_EXTRA_TEST_PROCESSES='{}'".format(",".join(extra)))

    data = report.get("data") or {}
    if data.get("recent_by_subdir"):
        lines.append(
            "# 最近写入集中在：{}".format(
                ", ".join(
                    "{}({})".format(key, value)
                    for key, value in sorted(
                        data["recent_by_subdir"].items(), key=lambda kv: -kv[1]
                    )[:5]
                )
            )
        )
    if data.get("other_candidate_dirs"):
        lines.append(
            "# 除 {} 外还存在这些目录，确认是否也算产出：{}".format(
                data.get("data_dir"), data["other_candidate_dirs"]
            )
        )

    topology = report.get("topology") or {}
    if len(topology.get("swarm_nodes") or []) > 1:
        lines.append("# ⚠️ 检测到多节点 swarm，需要先聚合各节点信号再给集群结论")
    if len(topology.get("compose_projects") or []) > 1:
        lines.append(
            "# 存在多个 compose project：{}，确认它们是否属于同一个集群编号".format(
                topology["compose_projects"]
            )
        )

    permissions = report.get("permissions") or {}
    if not permissions.get("docker_readable"):
        lines.append("# ⚠️ 当前账号读不到 docker，需要加入 docker 组或配置最小 sudo 规则")
    if not permissions.get("data_dir_readable"):
        lines.append("# ⚠️ 数据目录不可读，采集会一直 UNKNOWN")
    return lines


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    patterns = dict(DEFAULT_PATTERNS)
    containers = probe_containers(patterns)
    tester_names = [
        name
        for name in (containers.get("matched", {}).get("tester") or [])
        if any(
            item["name"] == name and item["state"].lower() == "running"
            for item in containers.get("containers", [])
        )
    ]
    report: Dict[str, Any] = {
        "probed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cluster_hint": os.getenv("HIL_MACHINE_NAME", ""),
        "permissions": probe_permissions(args.data_dir),
        "containers": containers,
        "processes": probe_processes(tester_names),
        "data": probe_data_dirs(args.data_dir, args.top, args.minutes),
        "topology": probe_topology(),
        "gpus": probe_gpus(),
    }
    report["suggested_config"] = suggest_config(report)
    return report


def print_human(report: Dict[str, Any]) -> None:
    def section(title: str) -> None:
        print("\n" + "─" * 68)
        print("  " + title)
        print("─" * 68)

    print("HIL2.0 现场只读探测报告　{}".format(report["probed_at"]))
    print("主机：{}".format(report["topology"].get("hostname", "?")))

    section("1. 权限")
    for key, value in report["permissions"].items():
        flag = "✓" if value is True else ("✗" if value is False else "·")
        print("  {} {:<22} {}".format(flag, key, value))

    section("2. 容器与角色匹配")
    containers = report["containers"]
    if containers.get("error"):
        print("  ✗ 读取失败：{}".format(containers["error"]))
    else:
        for item in containers["containers"]:
            roles = "/".join(item["matched_roles"]) or "-"
            print("  {:<32} {:<10} [{}]".format(item["name"][:32], item["state"], roles))
        if containers["missing_roles"]:
            print("\n  ⚠️ 未匹配到的角色：{}".format("、".join(containers["missing_roles"])))
        if containers["ambiguous"]:
            print("  ⚠️ 同时命中多个角色：{}".format(containers["ambiguous"]))
        if containers["unmatched"]:
            print("  · 未归类容器：{}".format(containers["unmatched"][:10]))

    section("3. tester 容器内进程（确认 pytest 之外的测试种类）")
    if not report["processes"]:
        print("  · 没有运行中的 tester 容器")
    for name, payload in report["processes"].items():
        if payload.get("error"):
            print("  ✗ {}：{}".format(name, payload["error"]))
            continue
        print("  {}（共 {} 个进程）".format(name, payload["process_count"]))
        for hit in payload["test_like"][:10]:
            print("    · {}".format(hit["line"]))
        if not payload["test_like"]:
            print("    · 未发现测试类进程")

    section("4. 数据目录")
    data = report["data"]
    if data.get("error"):
        print("  ✗ {}".format(data["error"]))
    else:
        print("  一级目录：{}".format(data.get("top_level", [])[:15]))
        print("  最近写入文件数：{}".format(data.get("recent_count", "?")))
        for key, value in sorted((data.get("recent_by_subdir") or {}).items(), key=lambda kv: -kv[1])[:8]:
            print("    · {:<24} {} 个".format(key, value))
        if data.get("other_candidate_dirs"):
            print("  其他候选产出目录：{}".format(data["other_candidate_dirs"]))

    section("5. 拓扑（一个集群编号是否含多节点）")
    for key, value in report["topology"].items():
        print("  {:<20} {}".format(key, value))

    section("6. GPU（仅参考，不参与判定）")
    gpus = report["gpus"]
    if not gpus.get("available"):
        print("  · 不可用：{}".format(gpus.get("error", "")))
    else:
        for row in gpus["rows"]:
            print("  {}".format(row))

    section("7. 建议配置（确认后写进 .env）")
    for line in report["suggested_config"]:
        print("  " + line)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="HIL2.0 现场只读探测")
    parser.add_argument("--data-dir", default=os.getenv("HIL_DATA_DIR", "/data1"))
    parser.add_argument("--minutes", type=int, default=15, help="最近写入的时间窗口")
    parser.add_argument("--top", type=int, default=20, help="每项最多列出多少条")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
