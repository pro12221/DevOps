# Helm 模版开发

## 文件结构

```text
wordpress/
  Chart.yaml          # 包含当前 chart 信息的 YAML 文件
  LICENSE             # 可选：包含 chart 的 license 的文本文件
  README.md           # 可选：一个可读性高的 README 文件
  values.yaml         # 当前 chart 的默认配置 values
  values.schema.json  # 可选：一个作用在 values.yaml 文件上的 JSON 模式
  charts/             # 包含该 chart 依赖的所有 chart 的目录
  crds/               # Custom Resource Definitions
  templates/          # 模板目录，与 values 结合使用时，将渲染生成 Kubernetes 资源清单文件
  templates/NOTES.txt # 可选：包含简短使用说明的文本文件
```

## Chart.yaml 文件

```yaml
apiVersion: chart API 版本 (必须)
name: chart 名 (必须)
version: SemVer 2 版本 (必须)
kubeVersion: 兼容的 Kubernetes 版本 (可选)
description: 一句话描述 (可选)
type: chart 类型 (可选)
keywords:
  - 当前项目关键字集合 (可选)
home: 当前项目的 URL (可选)
sources:
  - 当前项目源码 URL (可选)
dependencies: # chart 依赖列表 (可选)
  - name: chart 名称 (nginx)
    version: chart 版本 ("1.2.3")
    repository: 仓库地址 ("https://example.com/charts")
maintainers: # (可选)
  - name: 维护者名字 (对每个 maintainer 是必须的)
    email: 维护者的 email (可选)
    url: 维护者 URL (可选)
icon: chart 的 SVG 或者 PNG 图标 URL (可选)
appVersion: 包含的应用程序版本 (可选)。不需要 SemVer 版本
deprecated: chart 是否已被弃用 (可选, boolean)
```

## 父子 Chart（依赖）

一个 chart 可以依赖任意多个其它 chart，被依赖方称为 **子 chart（subchart / child chart）**，依赖方称为 **父 chart（parent chart）**。子 chart 通过 `Chart.yaml` 的 `dependencies` 声明，或直接手工放进 `charts/` 目录。

### 声明依赖

推荐在 `Chart.yaml` 里声明，再用 `helm dependency update` 拉取到 `charts/`：

```yaml
dependencies:
  - name: mysql
    version: 9.14.1
    repository: https://charts.bitnami.com/bitnami
    condition: mysql.enabled   # 布尔路径，控制是否启用
    tags:                      # 分组，便于批量开关
      - db
    alias: db                  # 同一 chart 引入多次时用于区分
```

- `repository` 也可写成 `@repo-name`，但需先 `helm repo add` 添加该仓库。
- `helm dependency update` 会把依赖以 `.tgz` 形式下载到 `charts/`；也可用 `helm pull` 手动放入（目录名不能以 `_` 或 `.` 开头）。

### values 覆盖与作用域

这是父子 chart 最容易踩坑的地方：

- 父 chart 用 **与子 chart 同名的顶层 key** 覆盖子 chart 的值。
- 子 chart 自身视角里 **没有这个前缀**（Helm 会剪掉命名空间前缀）。

```yaml
# 父 chart values.yaml
title: My Site
mysql:                # 与子 chart name 一致
  auth:
    password: secret
global:
  imageRegistry: registry.example.com
```

```yaml
# 子 chart (mysql) 模板中读取：
{{ .Values.auth.password }}          # = secret，前缀 mysql. 已被剪掉
{{ .Values.global.imageRegistry }}   # = registry.example.com
# 无法读取父 chart 的 .Values.title
```

作用域规则：

- 上层能读下层：父 chart 可用 `.Values.mysql.password` 访问子 chart 的值。
- 下层读不到上层：子 chart **不能**访问父 chart 的值（`global` 除外）。
- `global` 对所有（子）chart 同名可见；**父 chart 的 global 优先于子 chart 的 global**；子 chart 定义的 global 只向下传递，不会向上影响父 chart。

### 启用 / 禁用子 chart

```yaml
# 父 chart values.yaml
subchart1:
  enabled: true        # 对应 condition: subchart1.enabled
tags:
  front-end: false     # 批量关闭带该 tag 的子 chart
```

- `condition`：一个或多个逗号分隔的 YAML 路径，**取第一个有效路径**，其布尔值决定启用 / 禁用。
- `tags`：任一 tag 为 `true` 即启用该 chart。
- **条件覆盖 tag**：condition 命中时以 condition 为准。
- 两者都必须设置在 **顶层父 chart 的 values** 中；`tags:` 必须是顶层 key，不支持 global / 嵌套。
- 也可用 CLI 临时覆盖：`helm install --set tags.front-end=true --set subchart2.enabled=false`。

### 子 chart 值上浮（import-values）

若想让子 chart 的值作为父 chart 的默认值共享，可用 `import-values`：

```yaml
# 父 Chart.yaml
dependencies:
  - name: subchart
    version: 0.1.0
    repository: http://localhost:10191
    import-values:
      - data                  # exports 格式：取子 chart values.exports.data
      # - child: default.data   # child-parent 格式
      #   parent: myimports     # 复制到父 values 的 myimports
```

### 命名模板可见性

- 所有命名模板（`define`）共享 **全局命名空间**：父 chart 定义的可被子 chart 调用，反之亦然（子 chart 模板与顶层模板一起编译）。
- **同名冲突时后加载者生效**（不同 chart 版本同理）。
- 因此约定 **给模板名加 chart 名前缀**，如 `{{ define "mychart.labels" }}`；需要版本隔离时可带版本号 `mychart.v1.labels`。
- 跨 chart 调用用模板全名：`{{ include "parentchart.name" . }}`。

### 安装顺序

父子 chart 的所有 K8s 对象会 **聚合为同一个 release**，先按资源 Kind 排序、再按名称排序后统一创建 / 更新。因此顺序类似：

```text
Namespace(A) → Namespace(B) → Service(A) → Service(B) → ReplicaSet(B) → StatefulSet(A)
```

具体安装顺序由 Helm 源码 `kind_sorter.go` 中的 `InstallOrder` 枚举决定。

## CRD 说明

在 Helm 3 中，CRD 被视为一种特殊的对象，它们在 chart 部分之前被安装，并且会受到一些限制。CRD YAML 文件应该放置 chart 内的 `crds/` 目录下面。多个 CRDs 可以放在同一个文件中，Helm 将尝试将 CRD 目录中的所有文件加载到 Kubernetes 中。

## 命名模板

Helm 中，`templates/` 下以 `_` 开头的文件（如 `_helpers.tpl`）不会被当成 K8s 资源清单，而是用来定义可复用的命名模板片段（partials），其他模板可以通过 `include` 或 `template` 来调用它们。

一般来说，Helm 中约定将这些模板统一放到一个 partials 文件中，通常就是 `_helpers.tpl` 文件中。

## Chart Hooks

Helm 提供了 Hook 机制，允许 chart 开发人员在 release 生命周期的特定时间点介入。典型用途：

- 安装时、任何资源加载前，先加载 ConfigMap / Secret
- 安装新 chart 前运行 Job 备份数据库，升级后再运行 Job 还原
- 删除 release 前运行 Job，优雅停止关联服务

带 `helm.sh/hook` 注解的资源不再作为普通 release 资源，而是在对应生命周期点执行。

### Hook 类型

| Hook | 执行时机 |
|---|---|
| `pre-install` | 模板渲染后，任何资源创建**之前** |
| `post-install` | 所有资源加载到 Kubernetes **之后** |
| `pre-delete` | 删除请求发出后、任何资源被删除**之前** |
| `post-delete` | release 的所有资源被删除**之后** |
| `pre-upgrade` | 模板渲染后、资源更新**之前** |
| `post-upgrade` | 所有资源升级**之后** |
| `pre-rollback` | 模板渲染后、资源回滚**之前** |
| `post-rollback` | 所有资源修改**之后** |
| `test` | 执行 `helm test` 子命令时 |

一个资源可绑定多个生命周期，用逗号分隔：

```yaml
"helm.sh/hook": post-install,post-upgrade
```

> 注：Helm 3 已移除 `crd-install` hook，改由 `crds/` 目录处理。

### Hook 注解

- `helm.sh/hook`：声明资源属于哪种 hook（必需，否则视为普通 release 资源）。
- `helm.sh/hook-weight`：排序权重，**必须写成字符串**（可为负数）。
- `helm.sh/hook-delete-policy`：删除策略，可逗号分隔多个值。
- `helm.sh/resource-policy: keep`：希望永不删除的 hook 资源可加此注解。

### Hook 示例

`templates/post-install-job.yaml`：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: "{{ .Release.Name }}"
  labels:
    app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
    app.kubernetes.io/instance: {{ .Release.Name | quote }}
    app.kubernetes.io/version: {{ .Chart.AppVersion }}
    helm.sh/chart: "{{ .Chart.Name }}-{{ .Chart.Version }}"
  annotations:
    # 这一行把它定义为 hook；没有它，该 Job 会被当作 release 的一部分
    "helm.sh/hook": post-install
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": hook-succeeded
spec:
  template:
    metadata:
      name: "{{ .Release.Name }}"
      labels:
        app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
        app.kubernetes.io/instance: {{ .Release.Name | quote }}
        helm.sh/chart: "{{ .Chart.Name }}-{{ .Chart.Version }}"
    spec:
      restartPolicy: Never
      containers:
        - name: post-install-job
          image: "alpine:3.3"
          command: ["/bin/sleep", "{{ default "10" .Values.sleepyTime }}"]
```

### Hook 删除策略

hook 资源**不计入 release 清单**，`helm uninstall` 不会删除它们；因此必须依赖删除策略或 Job 的 `ttlSecondsAfterFinished` 来清理，否则会残留。

`helm.sh/hook-delete-policy` 支持的取值：

| 策略 | 含义 |
|---|---|
| `before-hook-creation` | 启动新 hook 前，先删除上一次的资源（**默认**） |
| `hook-succeeded` | hook 成功执行后删除资源 |
| `hook-failed` | hook 执行失败时删除资源 |

- 未指定任何策略时，默认行为等同 `before-hook-creation`。
- 需要永久保留的 hook 资源，加注解 `helm.sh/resource-policy: keep`。
- 多个策略可逗号分隔，例如 `"helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded`。

### 执行顺序

- 权重**必须是字符串**（可为正负），未设置时默认 `0`。
- 先按 **权重 → 资源 Kind → 名称** 升序排序，再从最小权重开始（负数 → 正数）依次执行。
- 同一 hook 中的多个资源**串行**执行。
- Helm 3.2.0 起，同权重资源按普通资源的顺序安装。
- `Job` / `Pod` 类型的 hook，Helm 会阻塞等待其完成；hook 失败会导致整个 release 失败。
- 子 chart 的 hook 总会执行，父 chart 无法禁用。