# Jenkins Kubernetes Cloud 与 Pod Template 配置详解

> 2026-09-29 · 适用于 kubernetes 插件 4557（chart 5.9.63 自带）· Jenkins 2.568.3
> 页面入口：`http://jenkins.devops.local:32037/manage/cloud/kubernetes/`
> 单实例只有一个 cloud：`kubernetes`（chart 自动创建，**不要删**），其下挂 Pod Template。
> 本文当前值全部取自本环境 `config.xml` 实况，非通用模板。

---

## 一、Cloud 列表页（/manage/cloud/）

| 列 | 含义 |
|---|---|
| Name | cloud 名，Jenkinsfile 里 `podCloud` / 多 cloud 时靠它区分。本环境只有一个 `kubernetes` |
| Controllers / Agents | 该 cloud 当前在线的 controller 数 / 正在跑的 agent pod 数 |
| 操作 | Configure（改配置）、Delete（删除 cloud。chart 建的删了 agent 就起不来了） |

点 `kubernetes` 进 Configure，即 Kubernetes Cloud Configuration 页。

---

## 二、Kubernetes Cloud Configuration（cloud 级字段）

### 1. 连接集群（本环境用 in-cluster SA，全免配）

| 字段 | 当前值 | 释义 |
|---|---|---|
| **Name** | `kubernetes` | cloud 标识。inline podTemplate 默认继承这里 |
| **Kubernetes URL** | `https://kubernetes.default.svc.cluster.local` | apiserver 地址。chart 已配好：controller pod 里的 SA + ClusterRoleBinding 免凭据直连。⚠️ 若此处留空 Jenkins 会试图自动探测（同集群可用，跨集群必须显式填） |
| **Kubernetes server certificate** | 空 | apiserver 的 CA 证书（PEM）。走 SA token 连本集群时 kube-system CA 自动挂载，不用填；跨集群 + https 时才需要 |
| **Disable https certificate check** | 否 | 勾上 = 跳过 TLS 校验（`skipTlsVerify`）。跨集群连自签 apiserver 时勾 |
| **Kubernetes Namespace** | `jenkins` | **agent pod 会被建到这个 ns**（不是 controller 所在 ns 的硬约束，是这里的值决定的）。同时 controller 的 RBAC 也只覆盖此 ns，改了 ns 要连 RBAC 一起改 |
| **Credentials** | 空 | 连 apiserver 的凭据。本集群走 SA 免密所以空。**跨集群（第二套 cloud）必须填**：prod 里建 SA + token，Secret text 存进来 |
| **WebSocket** | 否 | agent 回连方式。否 = 走 JNLP TCP 50000 端口（ jenkins-agent svc）；是 = 走 Jenkins HTTP 端口复用（agent 集群访问不到 50000 时才开，如公网/防火墙受限场景） |
| **Connection Timeout / Read Timeout** | 5 / 15 秒 | 对 apiserver 的 HTTP 连接超时/读超时。apiserver 慢再调，一般不动 |
| **Max requests per host** | 32 | 对 apiserver 的并发 HTTP 请求数。按需调大，防止 agent 突发时 apiserver 限流 |

### 2. Jenkins 与 agent 的连接（controller 被连的通道）

| 字段 | 当前值 | 释义 |
|---|---|---|
| **Jenkins URL** | `http://jenkins.jenkins.svc.cluster.local:8080` | agent pod 回连 controller 的地址。**同集群场景保持 svc 域名即可**。⚠️ 跨集群 cloud 必须改成对端可达的地址（如 `http://123.58.219.112:32037`），否则 agent 起来了也连不回 |
| **Jenkins tunnel** | `jenkins-agent.jenkins.svc.cluster.local:50000` | JNLP 通道（agent 主动连 controller 的 50000）。同集群保持 svc。跨集群要改对端可达地址，或改走 WebSocket |
| **Container Cap** | 10 | 该 cloud 同时存在的**容器**数上限（pod 数 × 容器数的总和）。0 = 无限。防 agent 失控刷爆集群 |
| **Pod Labels** | 有（chart 注入） | 给每个 agent pod 附加的标签，用于审计/NetworkPolicy 匹配 |
| **Defaults Provider Template** | 空（2026-09-29 已清空） | inline podTemplate **未指定某字段时的取值来源模板**。曾悬空引用 `test`（不存在）导致告警，已清空修复 |

> 本环境结论：cloud 级配置只需修一处——Defaults Provider Template 清空（✅ 2026-09-29 已做，原悬空引用 `test`）。其余字段保持 chart 值即可。

---

## 三、Pod Template（点 cloud 里的 Pod Templates → Add / 编辑）

> 页面分左右两栏：左 = Pod 级（元数据/调度/卷），右 = Containers 列表（jnlp + 业务容器）。每个字段对应 config.xml `<PodTemplate>` 一个标签。

### 1. Pod 级字段（左栏）

| 字段 | 当前值 | 释义 |
|---|---|---|
| **Name** | `default` | 模板名（UI 标识）。引用靠 label，不靠 name |
| **Labels** | `jenkins-jenkins-agent` | **job 的引用键**：pipeline `agent { label 'xxx' }` 匹配这里的值。改它 = 改 Jenkinsfile 引用 |
| **Namespace** | `jenkins` | 该模板 pod 建 ns（可覆盖 cloud 级设置） |
| **Usage** | `Normal` | `Only build jobs with planner restrictions` = 仅显式声明 label 的 job 能用，防止误占 |
| **Pod Retention** | `Never` | build 结束后 pod 处置：Never 立删 / OnFailure 失败留 / Always 留（排障用） |
| **Container Cap** | 空 | 该模板允许的容器上限（0=无限） |
| **Show raw yaml** | — | 界面直接看/改生成的 pod yaml，等价 Config File Provider，排障利器 |
| **Idle minutes** | 0 | agent 闲置 N 分钟才删。0 = job 完即删（省资源，正常设置） |
| **Active Deadline Seconds** | 0 | pod 最大存活时长（防僵死 agent）。0 = 不限 |
| **Service Account** | `default` | agent pod 的 SA。**agent 本身无需集群权限**（deploy 走 prod-kubeconfig 凭据），保持 default |
| **Node Selector** | 空 | 钉 pod 到特定节点。本环境 **devops-m1 有 control-plane:NoSchedule 污点**，模板又没配 toleration → agent 实际只会落 devops-w1，无需再配 |
| **Taints/Tolerations** | 空 | 要让 agent 上 m1 才需要填 toleration。本环境不需要 |
| **YAML** | 空 | 整个模板可用一段 K8s Pod yaml 定义（和 UI 字段二选一，UI 优先）。高级用法 |
| **Volumes → Add Volume** | 空 | 三种：HostPath（节点目录）/ PVC / **Secret Volume（最常用：挂凭据）**。kaniko 场景挂 `harbor-push-config` → `/kaniko/.docker` |
| **Inherited templates/volumes** | — | 5.x 新特性：从其它模板继承字段，组合复用 |

### 2. Container Template（右栏 → Add Container）

> jnlp 容器由 chart 预建：`jenkins/inbound-agent:3385...`，**command 留空、args `${computer.jnlpmac} ${computer.name}`，这行是 agent 回连控制器的握手参数，改了就废**。业务容器（kaniko/maven/kubectl）在这之后追加。

| 字段 | 释义 |
|---|---|
| **Name** | 容器名。**特殊值 `jnlp` = 替换默认 agent 容器**；其它名（如 `kaniko`）= 追加 sidecar。pipeline 里 `container('名字')` 进对应容器执行 |
| **Docker image** | 镜像。想每次构建拉最新勾 always pull |
| **Command / Arguments** | 容器入口覆盖。long-running sidecar 的套路：Command `sleep`、Arguments `9999999`（kaniko debug 镜像即如此），让容器常驻供 pipeline exec |
| **Working directory** | workspace 路径。同一 pod 内所有容器共享（emptyDir），默认 `/home/jenkins/agent` |
| **TTY** | 勾上分配 tty，sleep 常驻容器的必备搭配（无 tty 的 sleep 进程会被 SIGHUP 立杀） |
| **Run with privileged** | 特权容器。kaniko **不需要**；DinD 必须 |
| **Environment variables** | 注入 env。可引用 Jenkins 凭据（secret text/password 屏蔽） |
| **Resource requests/limits** | 容器资源。jnlp 默认 512m/512Mi；构建容器按需，kaniko 大镜像建议 cpu 1 / memory 2Gi 起 |
| **Ports / Liveness probe** | 一般留空。sidecar 起服务（如 dind 2375、redis 供测试）才需要 |

### 3. 模板示例

**实作（本环境真实存在的模板，2026-09-29 建，BuildKit 路线）**——`buildkit`，Jenkinsfile 用 `agent { label 'buildkit' }` 引用：

```text
Name: buildkit
Labels: buildkit
Namespace: jenkins
Containers:
  Name: buildctl                        # 追加 sidecar，不动 jnlp
  Image: moby/buildkit:v0.33.0-rootless # rootless 镜像（UID 1000，HOME=/home/user）
  Command: sleep
  Arguments: 9999999
  TTY: 勾选
  Working directory: /home/jenkins/agent
Volumes → Add:
  Secret Volume: harbor-push-config → /home/user/.docker   # ⚠️ 挂到 buildctl 容器！
```

> ⚠️ 关键：BuildKit 认证走 gRPC session——buildctl **客户端**把 ~/.docker/config.json 的凭据喂给 daemon。secret 必须挂到 agent 的 buildctl 容器（/home/user/.docker），只挂 buildkitd daemon 侧照样 401。详见《流水线部署步骤.md》第七步。

**kaniko 示例（未实施，kaniko 已 EOL，留作对照）**（对应方式 B）：

UI 操作路径：Pod Templates → Add Pod Template，随后逐项填：

```text
Name: kaniko-builder
Labels: kaniko                      # Jenkinsfile 用 agent { label 'kaniko' } 引用
Namespace: jenkins
Usage: Normal
Service Account: default
Containers → Add:
  Name: kaniko                      # 追加 sidecar，不动 jnlp
  Image: gcr.io/kaniko-project/executor:debug
  Command: sleep
  Arguments: 9999999
  TTY: 勾选
  Limits: cpu 1 / memory 2Gi
Volumes → Add:
  Secret Volume: harbor-push-config → /kaniko/.docker
```

Jenkinsfile 侧引用：

```groovy
podTemplate(label: 'kaniko',
    containers: [containerTemplate(name: 'kaniko', image: 'gcr.io/kaniko-project/executor:debug',
        command: 'sleep', args: '9999999', ttyEnabled: true)],
    volumes: [secretVolume(secretName: 'harbor-push-config', mountPath: '/kaniko/.docker')]) {
  node('kaniko') { container('kaniko') { sh '/kaniko/executor ...' } }
}
```

UI 预定义（方式 B）与 Jenkinsfile inline（方式 A）二选一即可，**inline 优先级更高**——同 label 冲突时以流水线声明的为准。

---

## 四、验证模板能跑

```bash
# 1. 建一个测试 pipeline（agent 用模板 label）
pipeline {
  agent { label 'jenkins-jenkins-agent' }
  stages { stage('t') { steps { sh 'hostname && echo ok' } } }
}
# 2. 触发同时观察 agent pod（在 devops-w1 起，结束即删）
kubectl --context devops -n jenkins get pods -w
```

排障速查：

| 症状 | 原因 |
|---|---|
| agent pod 一直 Pending | ns 资源配额 / 无节点可调度（污点） / 镜像拉不动 |
| agent 起来秒退（jnlp 报 connect refused） | Jenkins URL / tunnel 不可达；jnlp args 被改坏 |
| `Pending: node taints` | 模板没配 toleration 又只有 master（本环境不适用——w1 无污点） |
| kaniko 报 unauthorized | secret 没挂到 /kaniko/.docker；secret 的 key 不是 `config.json`（docker-registry 类型默认 key 是 `.dockerconfigjson`，插件 secretVolume 无法重命名）；或用户名没带 `robot$demo+` 前缀（Harbor 2.15 格式） |
| buildctl 报 401 unauthorized | secret 只挂了 buildkitd daemon、没挂 agent 的 buildctl 容器（BuildKit 认证走 gRPC session，凭据在客户端侧，见上文实作模板） |
| 流水线卡 "Waiting for agent" | label 不匹配任何模板；或 Defaults Provider Template 悬空告警 |
