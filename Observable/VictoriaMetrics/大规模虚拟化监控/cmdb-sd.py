#!/usr/bin/env python3
"""cmdb-sd.py — 从 CMDB 拉取目标，生成 Prometheus/vmagent file_sd 文件

用法:
  ./cmdb-sd.py                    # 输出到 TARGETS_DIR 下的多个 file_sd 文件

依赖: 仅 Python 3 标准库
api-key 通过环境变量 CMDB_API_KEY 提供，不写入代码

目标范围:
  - 宿主机 (category=9  Server):   一级业务 Usage==uvmp 且 OperationalStatus==上线
  - 虚机   (category=16 VMServer): 全部非删除虚机（状态只作为展示标签，不过滤）
  - IDC 变量可再按机房收窄（留空 = 全部机房）

标签:
  - 宿主机: type=host, idc(逻辑机房), biz1(一级业务), biz2(二级业务)
  - 虚机:   type=vm,   idc, biz1, biz2, owner(所有者), status(状态,展示用),
            host_ip(对应宿主机), os(windows/linux, 决定探测端口: 22 或 3389)
"""

import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

# ===== 配置（随时可改，均可用同名环境变量覆盖）=====
API_BASE = os.environ.get("CMDB_API_BASE", "http://api-gw.ucloudadmin.com/cmdb")
API_KEY = os.environ.get("CMDB_API_KEY", "")
PER_PAGE = int(os.environ.get("PER_PAGE", "500"))

# CMDB RSQL 语法，多个条件用 ; 分隔。留空 = 不过滤（拉全量）
HOST_QUERY = os.environ.get("HOST_QUERY", "OperationalStatus==上线;Usage==uvmp")  # 宿主机: 上线 + 一级业务 uvmp
VM_QUERY = os.environ.get("VM_QUERY", "")                                       # 虚机: 全部（状态只做展示标签）

# 机房过滤: 留空 = 全部机房；填机房名 = 只保留该机房（与 IDC 标签值精确匹配）
IDC = os.environ.get("IDC", "")
TESTING_IDC_PREFIX = os.environ.get("TESTING_IDC_PREFIX", "TEST")
TARGETS_DIR = os.environ.get("TARGETS_DIR", "/data/targets")

if not TESTING_IDC_PREFIX:
    raise RuntimeError("必须设置非空的 TESTING_IDC_PREFIX")

CATEGORY_HOST = 9   # Server 宿主机
CATEGORY_VM = 16    # VMServer 虚机

HOST_FIELDS = "IP,IDC,Usage,SecUsage,OperationalStatus"
VM_FIELDS = "IP,IDC,Usage,SecUsage,Owner,BelongsTo,OperationalStatus,OS"


def fetch_all(category, q, fields):
    """分页拉取某一 category 的全部记录，返回 list[dict]（每条一条 CI 原始记录）"""
    records = []
    page = 1
    total = 1
    while True:
        params = {
            "category": str(category),
            "perPage": str(PER_PAGE),
            "page": str(page),
            "fields": "({})".format(fields),
        }
        if q:
            params["q"] = q
        url = "{}/v2/ci/search?{}".format(API_BASE, urllib.parse.urlencode(params))
        # 注意: GET 含中文 RSQL 时不能设 Content-Type: application/json (cmdb 编码问题)
        req = urllib.request.Request(url, headers={"api-key": API_KEY})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("meta", {}).get("code") != 200:
            raise RuntimeError("CMDB 返回错误: {}".format(body))
        if page == 1:
            total = body.get("meta", {}).get("total") or 0
        records.extend(body.get("data") or [])
        if page * PER_PAGE >= total:
            break
        page += 1
    return records


def host_entry(r, environment):
    """宿主机记录 -> file_sd 条目"""
    return {
        "targets": [r["IP"]],
        "labels": {
            "type": "host",
            "environment": environment,
            "idc": r.get("IDC") or "",
            "biz1": r.get("Usage") or "",
            "biz2": r.get("SecUsage") or "",
            "os": "linux",
        },
    }


def vm_entry(r, environment):
    """虚机记录 -> file_sd 条目"""
    os_name = "windows" if "win" in (r.get("OS") or "").lower() else "linux"
    return {
        "targets": [r["IP"]],
        "labels": {
            "type": "vm",
            "environment": environment,
            "idc": r.get("IDC") or "",
            "biz1": r.get("Usage") or "",
            "biz2": r.get("SecUsage") or "",
            "owner": r.get("Owner") or "",
            "status": r.get("OperationalStatus") or "",
            "os": os_name,
            "host_ip": r.get("BelongsTo") or "",
        },
    }


def write_targets(path, entries):
    """原子写入 file_sd 文件，避免 vmagent 读到半截文件。"""
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".targets.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def classify_environment(record):
    idc = (record.get("IDC") or "").strip()
    if idc.startswith(TESTING_IDC_PREFIX):
        return "testing"
    if idc:
        return "production"
    return None


def main():
    hosts = [r for r in fetch_all(CATEGORY_HOST, HOST_QUERY, HOST_FIELDS) if r.get("IP")]
    vms = [r for r in fetch_all(CATEGORY_VM, VM_QUERY, VM_FIELDS) if r.get("IP")]

    if IDC:
        hosts = [r for r in hosts if r.get("IDC") == IDC]
        vms = [r for r in vms if r.get("IDC") == IDC]

    environment_hosts = {"production": [], "testing": []}
    environment_vms = {"production": [], "testing": []}
    blackbox = []
    for record in hosts:
        environment = classify_environment(record)
        if environment:
            environment_hosts[environment].append(record)
            blackbox.append((record, environment))
    for record in vms:
        environment = classify_environment(record)
        if environment:
            environment_vms[environment].append(record)
            blackbox.append((record, environment))

    outputs = {
        "production-node.json": [host_entry(r, "production") for r in environment_hosts["production"]],
        "production-libvirt.json": [host_entry(r, "production") for r in environment_hosts["production"]],
        "testing-node.json": [host_entry(r, "testing") for r in environment_hosts["testing"]],
        "testing-libvirt.json": [host_entry(r, "testing") for r in environment_hosts["testing"]],
        "blackbox-icmp.json": [
            {"targets": [r["IP"]], "labels": {"environment": environment, "type": r.get("type", "target")}}
            for r, environment in blackbox
        ],
        "blackbox-tcp.json": [
            {"targets": [r["IP"]], "labels": {
                "environment": environment,
                "type": r.get("type", "target"),
                "os": "windows" if "win" in (r.get("OS") or "").lower() else "linux",
            }}
            for r, environment in blackbox
        ],
    }
    for filename, entries in outputs.items():
        write_targets(os.path.join(TARGETS_DIR, filename), entries)

    print("生成生产宿主机 {} 台、测试宿主机 {} 台、生产虚机 {} 台、测试虚机 {} 台；目标目录 {}".format(
        len(environment_hosts["production"]), len(environment_hosts["testing"]),
        len(environment_vms["production"]), len(environment_vms["testing"]), TARGETS_DIR))


if __name__ == "__main__":
    main()
