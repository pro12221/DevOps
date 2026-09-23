#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

import json                 # 解析 CMDB API 返回的 JSON / 把 file_sd 条目序列化成 JSON 文件
import os                  # 读取环境变量、路径拼接、创建目录、修改文件权限
import sys                 # 标准库（保留备用，当前主流程未直接使用）
import tempfile            # 生成临时文件，配合 os.replace 实现 file_sd 原子写入
import urllib.parse        # 把请求参数编码成 URL query string（含中文 RSQL 的安全编码）
import urllib.request      # 发送 HTTP GET 请求调用 CMDB API

# ===== 配置（随时可改，均可用同名环境变量覆盖）=====
# CMDB API 网关地址，默认内网网关，可用环境变量 CMDB_API_BASE 覆盖
API_BASE = os.environ.get("CMDB_API_BASE", "http://api-gw.ucloudadmin.com/cmdb")
# API 密钥：只从环境变量 CMDB_API_KEY 读取，绝不硬编码到代码/仓库里
API_KEY = os.environ.get("CMDB_API_KEY", "")
# 分页大小：每页拉多少条记录，默认 500（调大可减少请求次数，注意 API 上限）
PER_PAGE = int(os.environ.get("PER_PAGE", "500"))

# CMDB RSQL 语法，多个条件用 ; 分隔。留空 = 不过滤（拉全量）
# 宿主机过滤条件：运维状态==上线 且 一级业务==uvmp（即 UVMP 虚机平台管理的宿主机）
HOST_QUERY = os.environ.get("HOST_QUERY", "OperationalStatus==上线;Usage==uvmp")
# 虚机过滤条件：空 = 不过滤，拉全部虚机（运维状态只作为展示标签，不做剔除依据）
VM_QUERY = os.environ.get("VM_QUERY", "")

# 机房过滤: 留空 = 全部机房；填机房名 = 只保留该机房（与 IDC 标签值精确匹配）
IDC = os.environ.get("IDC", "")
# 测试机房名前缀：IDC 以此开头的归入 testing 环境，其余非空 IDC 归入 production
TESTING_IDC_PREFIX = os.environ.get("TESTING_IDC_PREFIX", "TEST")
# file_sd 输出目录，vmagent 的 file_sd_configs 会 watch 这个目录
TARGETS_DIR = os.environ.get("TARGETS_DIR", "/data/targets")

# 测试机房前缀必须非空：否则全部机器会被误判为生产环境，护栏检查
if not TESTING_IDC_PREFIX:
    raise RuntimeError("必须设置非空的 TESTING_IDC_PREFIX")

CATEGORY_HOST = 9   # CMDB CI 类型编号：9 = Server（物理宿主机）
CATEGORY_VM = 16    # CMDB CI 类型编号：16 = VMServer（虚拟机）

# 拉取宿主机时向 CMDB 指定的字段列表（减少响应体积），字段含义见下文引用处
HOST_FIELDS = "IP,IDC,Usage,SecUsage,OperationalStatus"
# 拉取虚机时额外多要 Owner(所有者)/BelongsTo(所属宿主机)/OS(操作系统) 三个字段
VM_FIELDS = "IP,IDC,Usage,SecUsage,Owner,BelongsTo,OperationalStatus,OS"


def fetch_all(category, q, fields):
    """分页拉取某一 category 的全部记录，返回 list[dict]（每条一条 CI 原始记录）"""
    records = []          # 累积器：收集所有分页返回的 CI 记录
    page = 1              # 当前页码，从 1 开始
    total = 1             # 总记录数（先随便给个非 0 值，保证至少进一次循环）
    while True:           # 不断翻页，直到取满 total 条
        # 组装 query 参数：类型、每页条数、页码、以及要返回的字段列表
        params = {
            "category": str(category),        # CI 类型（9=宿主机 / 16=虚机）
            "perPage": str(PER_PAGE),         # 每页条数
            "page": str(page),                # 当前页码
            "fields": "({})".format(fields),   # CMDB 要求字段列表用括号包裹
        }
        if q:                                # 有过滤条件时才带上 q 参数（空则拉全量）
            params["q"] = q
        # urlencode 会把中文 RSQL（如"上线"）安全编码进 URL
        url = "{}/v2/ci/search?{}".format(API_BASE, urllib.parse.urlencode(params))
        # 注意: GET 含中文 RSQL 时不能设 Content-Type: application/json (cmdb 编码问题)
        # 只在请求头带 api-key 做认证，不设 Content-Type
        req = urllib.request.Request(url, headers={"api-key": API_KEY})
        with urllib.request.urlopen(req, timeout=30) as resp:   # 发 GET，30s 超时
            body = json.loads(resp.read().decode("utf-8"))     # 响应体按 UTF-8 解成 dict
        # CMDB 约定：业务码在 meta.code，200 才算成功
        if body.get("meta", {}).get("code") != 200:
            raise RuntimeError("CMDB 返回错误: {}".format(body))
        # 第一页时读取总记录数，用于计算还需要翻几页
        if page == 1:
            total = body.get("meta", {}).get("total") or 0
        records.extend(body.get("data") or [])  # 追加本页数据；data 为空时安全跳过
        if page * PER_PAGE >= total:            # 已拉取条数覆盖总数 -> 结束翻页
            break
        page += 1                               # 否则继续拉下一页
    return records                              # 返回该类型的全部原始记录


def host_entry(r, environment):
    """宿主机记录 -> file_sd 条目"""
    return {
        "targets": [r["IP"]],          # 采集目标地址 = 宿主机 IP（node_exporter 等监听的机器）
        "labels": {
            "type": "host",            # 目标类型：物理宿主机
            "environment": environment,  # 环境：production / testing（按 IDC 前缀划分）
            "idc": r.get("IDC") or "",   # 逻辑机房名（get+or 兜底：字段缺失时给空串）
            "biz1": r.get("Usage") or "",      # 一级业务（如 uvmp）
            "biz2": r.get("SecUsage") or "",   # 二级业务
            "os": "linux",             # 宿主机统一按 Linux 处理
        },
    }


def vm_entry(r, environment):
    """虚机记录 -> file_sd 条目"""
    # OS 字段包含 "win"（不区分大小写）即认为是 Windows，否则按 Linux
    os_name = "windows" if "win" in (r.get("OS") or "").lower() else "linux"
    return {
        "targets": [r["IP"]],          # 采集目标地址 = 虚机 IP
        "labels": {
            "type": "vm",              # 目标类型：虚拟机
            "environment": environment,  # 环境：production / testing
            "idc": r.get("IDC") or "",          # 逻辑机房名
            "biz1": r.get("Usage") or "",       # 一级业务
            "biz2": r.get("SecUsage") or "",    # 二级业务
            "owner": r.get("Owner") or "",      # 虚机所有者（展示用，便于找人）
            "status": r.get("OperationalStatus") or "",  # 运维状态（展示用，不过滤）
            "os": os_name,             # windows/linux：决定后续拨测端口 3389 还是 22
            "host_ip": r.get("BelongsTo") or "",  # 该虚机所在的宿主机 IP（关联层叠图表）
        },
    }


def blackbox_entry(r, environment, target_type):
    """CMDB 记录 -> Blackbox file_sd 条目，保留 Grafana 所需业务标签。"""
    # blackbox 拨测条目的公共标签（host/vm 共有）
    labels = {
        "type": target_type,               # 目标类型：host / vm
        "environment": environment,        # 环境：production / testing
        "idc": r.get("IDC") or "",         # 逻辑机房名
        "biz1": r.get("Usage") or "",      # 一级业务
        "biz2": r.get("SecUsage") or "",   # 二级业务
        "os": "windows" if "win" in (r.get("OS") or "").lower() else "linux",  # 判断 OS 类型
    }
    if target_type == "vm":                # 虚机额外补充三个标签（宿主机没有）
        labels.update({
            "owner": r.get("Owner") or "",               # 所有者
            "status": r.get("OperationalStatus") or "",  # 运维状态
            "host_ip": r.get("BelongsTo") or "",         # 所属宿主机 IP
        })
    # 拨测目标就是该机器的 IP；标签随目标一起写入
    return {"targets": [r["IP"]], "labels": labels}


def write_targets(path, entries):
    """原子写入 file_sd 文件，避免 vmagent 读到半截文件。"""
    out_dir = os.path.dirname(os.path.abspath(path))  # 取目标文件的绝对目录
    os.makedirs(out_dir, exist_ok=True)               # 目录不存在则创建（已存在不报错）
    # 在同目录建临时文件（同目录保证 os.replace 是原子操作，不跨文件系统）
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".targets.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:                 # 用文件描述符打开临时文件准备写入
            json.dump(entries, f, ensure_ascii=False)  # 序列化写入；中文标签不转义
            f.write("\n")                            # 末尾补换行，符合 POSIX 文本习惯
        os.chmod(tmp, 0o644)                          # 开放读权限，vmagent(非 root) 可读
        os.replace(tmp, path)                         # 原子替换：旧文件瞬间换成新文件
    finally:
        # 兜底清理：正常流程 os.replace 后 tmp 已不存在；异常时删除残留临时文件
        if os.path.exists(tmp):
            os.remove(tmp)


def classify_environment(record):
    """按 IDC 前缀把记录归入 testing / production，空 IDC 返回 None（丢弃）。"""
    idc = (record.get("IDC") or "").strip()   # 取机房名并去首尾空白；缺失时为空串
    # 测试机房以 TEST 开头（前缀可由 TESTING_IDC_PREFIX 环境变量覆盖）
    if idc.startswith(TESTING_IDC_PREFIX):   # 前缀匹配 -> 测试环境
        return "testing"
    if idc:                                  # 有机房名但不是测试前缀 -> 生产环境
        return "production"
    return None                              # 无机房信息 -> 无法分类，调用方丢弃


def main():
    # 拉全量宿主机 + 虚机，过滤无 IP 的脏数据
    # 宿主机：按 HOST_QUERY 过滤后拉全量，再剔除 IP 字段为空的脏记录
    hosts = [r for r in fetch_all(CATEGORY_HOST, HOST_QUERY, HOST_FIELDS) if r.get("IP")]
    # 虚机：无过滤条件拉全量，同样剔除无 IP 的脏记录
    vms = [r for r in fetch_all(CATEGORY_VM, VM_QUERY, VM_FIELDS) if r.get("IP")]

    # IDC 变量非空时按机房收窄范围
    if IDC:
        hosts = [r for r in hosts if r.get("IDC") == IDC]  # 只保留指定机房的宿主机
        vms = [r for r in vms if r.get("IDC") == IDC]      # 只保留指定机房的虚机

    # 按环境分桶；blackbox 列表同时记录目标类型(host/vm)，虚机和宿主机都要拨测
    environment_hosts = {"production": [], "testing": []}  # 宿主机按环境分桶
    environment_vms = {"production": [], "testing": []}    # 虚机按环境分桶
    blackbox = []                                          # 待拨测列表: (记录, 环境, 类型)
    for record in hosts:                   # 逐台宿主机分类
        environment = classify_environment(record)  # 按 IDC 前缀判定环境
        if environment:                             # None(无 IDC) 的脏数据直接跳过
            environment_hosts[environment].append(record)      # 入对应环境的宿主机桶
            blackbox.append((record, environment, "host"))     # 宿主机也加入拨测列表
    for record in vms:                     # 逐台虚机分类（逻辑同上）
        environment = classify_environment(record)
        if environment:
            environment_vms[environment].append(record)        # 入对应环境的虚机桶
            blackbox.append((record, environment, "vm"))       # 虚机加入拨测列表

    # 六个 file_sd 文件: node/libvirt 按环境拆分，blackbox 两个文件包含全部环境
    # （环境的过滤交给 vmagent relabel 的 keep 规则，文件本身不拆，减少重复）
    outputs = {
        # 生产宿主机 -> node_exporter 采集目标文件
        "production-node.json": [host_entry(r, "production") for r in environment_hosts["production"]],
        # 生产宿主机 -> libvirt 采集目标文件（与 node 同目标，采集作业不同）
        "production-libvirt.json": [host_entry(r, "production") for r in environment_hosts["production"]],
        # 测试宿主机 -> node_exporter 采集目标文件
        "testing-node.json": [host_entry(r, "testing") for r in environment_hosts["testing"]],
        # 测试宿主机 -> libvirt 采集目标文件
        "testing-libvirt.json": [host_entry(r, "testing") for r in environment_hosts["testing"]],
        # ICMP 拨测目标（全部环境、宿主机+虚机混在一个文件里）
        "blackbox-icmp.json": [
            blackbox_entry(r, environment, target_type)
            for r, environment, target_type in blackbox
        ],
        # TCP 端口拨测目标（同上，具体端口由 vmagent relabel 按 os 标签决定）
        "blackbox-tcp.json": [
            blackbox_entry(r, environment, target_type)
            for r, environment, target_type in blackbox
        ],
    }
    for filename, entries in outputs.items():       # 逐个文件写出
        write_targets(os.path.join(TARGETS_DIR, filename), entries)  # 拼绝对路径原子写入

    # 运行结果摘要：各环境宿主机/虚机数量 + 输出目录，方便 cron 日志留痕
    print("生成生产宿主机 {} 台、测试宿主机 {} 台、生产虚机 {} 台、测试虚机 {} 台；目标目录 {}".format(
        len(environment_hosts["production"]), len(environment_hosts["testing"]),
        len(environment_vms["production"]), len(environment_vms["testing"]), TARGETS_DIR))


if __name__ == "__main__":      # 直接运行本脚本时执行 main（被 import 时不执行）
    main()
