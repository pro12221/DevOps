# 02 Jenkins Pipeline 语法与配置

> 2026-09-30 · 本章 = **Pipeline 怎么写（语法）+ Jenkins 怎么配（Cloud/模版/凭据/REST）**
> 环境实况：Jenkins 2.568.3-jdk21（chart 5.9.63，ns jenkins）· kubernetes 插件 4557 · gitlab-plugin 1.2154 · cloud `kubernetes`（chart 自动建，默认 label `jenkins-jenkins-agent`）
> 章节导航：[01-环境搭建.md] → **本章** → [03-GitLab配置.md] → [04-整体流程详解.md]

---

## 一、执行模型：controller、agent 与 job

### 1. controller 与 agent

| 概念 | 本环境对应 |
|---|---|
| controller | StatefulSet `jenkins-0`（devops-m1），只做调度/UI/REST，不跑构建 |
| agent（node） | **动态 K8s pod**：job 声明 `agent { label 'xxx' }` → 匹配 cloud 里同 label 的 PodTemplate → 在 ns jenkins 起 pod → 结束即删 |
| jnlp 容器 | agent pod 里负责与 controller 通信的容器，chart 预置（`jenkins/inbound-agent`），**command/args 别动** |
| 业务容器 | 模版里追加的 sidecar（如 `buildctl`），pipeline 用 `container('名字')` 进去执行 |

调度链：`agent { label 'buildkit' }` → 找 label 匹配的模板 → cloud `kubernetes` 调 apiserver 建 pod → jnlp 回连 controller（`jenkins-agent.jenkins.svc:50000`）→ job 的 steps 派给 pod 执行。

### 2. job 怎么拿到 Jenkinsfile

| 方式 | 机制 | 适用 |
|---|---|---|
| Pipeline script（贴 UI） | 定义存 job config.xml | 10 行试验 |
| **Pipeline script from SCM（本环境）** | job 存仓库地址，每次构建先拉 Jenkinsfile 再执行，Jenkinsfile 与代码同版本演进 | 正式流水线 |

from SCM 两个关键开关（本环境 job `demo-app` 实况）：

- **lightweight checkout**：只 fetch Jenkinsfile 单文件，省时省带宽。代价是**拿不到 git sha**（构建号 tag `b$BUILD_NUMBER` 没带 sha 的原因；要 sha 得关掉它或额外 `git` 步骤）。
- 声明式默认**隐式 "Declarative: Checkout SCM" stage**：每个 agent 起来后先全量 checkout 到 `$WORKSPACE`——这就是为什么 Jenkinsfile 里没写 `git`/`checkout` 步骤，`$WORKSPACE` 里却有代码可供 buildctl 用。想关掉：`options { skipDefaultCheckout() }`。

## 二、声明式语法逐段详解（以本环境真实 Jenkinsfile 为例）

```groovy
pipeline {
  agent { label 'buildkit' }
  options {
    disableConcurrentBuilds()
    gitLabConnection('gitlab')
  }
  triggers { gitlab(triggerOnPush: true, triggerOnMergeRequest: false, branchFilterType: 'All', secretToken: 'gltok-demo-2026') }
  environment {
    HARBOR   = 'harbor.devops.local:32037'
    PROJECT  = 'demo'
    APP      = 'demo-app'
    BUILDKIT = 'tcp://buildkitd.jenkins.svc.cluster.local:1234'
  }
  stages {
    stage('Build & Push (BuildKit)') {
      steps {
        gitlabCommitStatus(name: 'build') {
          container('buildctl') {
            sh '''
              buildctl --addr "$BUILDKIT" build \\
                --frontend dockerfile.v0 \\
                --local context="$WORKSPACE" \\
                --local dockerfile="$WORKSPACE" \\
                --output "type=image,name=$HARBOR/$PROJECT/$APP:b$BUILD_NUMBER,push=true,registry.insecure=true" \\
                --export-cache "type=registry,ref=$HARBOR/$PROJECT/cache:$APP" \\
                --import-cache "type=registry,ref=$HARBOR/$PROJECT/cache:$APP"
            '''
          }
        }
      }
    }
  }
}
```

### 1. `pipeline { }` 与 `agent`

- 声明式的唯一顶层块，Jenkins 先做静态检查（字段拼错直接构建前失败，这是比 scripted 强的地方）。
- `agent` 三种常用：`any`（任意可用节点）/ `none`（stage 级各自声明）/ **`label 'xxx'`（本环境）**——label 是 K8s cloud 下选模版的唯一钥匙。
- 也可以不建预定义模版，直接在 Jenkinsfile 里 `podTemplate(label: 'xxx', containers: [...], volumes: [...]) { node('xxx') {...} }` inline 声明（见 §三.5 优先级）。

### 2. `options { }`（job 级行为开关）

| 写法 | 作用 | 备注 |
|---|---|---|
| `disableConcurrentBuilds()` | 同 job 不并发 | 构建号=镜像 tag，防并发抢号 |
| `gitLabConnection('gitlab')` | 给本次构建绑定全局 GitLab 连接 | 供 `gitlabCommitStatus` 调 API，配置见 03 章四.2 |
| `skipDefaultCheckout()` | 关掉隐式全量 checkout | 不需要代码的 job 用 |
| `buildDiscarder(logRotator(numToKeepStr: '30'))` | 构建历史保留策略 | 长期跑的 job 必配，否则磁盘吃满 |
| `timeout(time: 30, unit: 'MINUTES')` | 整条流水线超时杀 | 防 agent 僵死 |
| `timestamps()` | 日志加时间戳 | ⚠️ **timestamper 插件没装就别写**——本环境 #1 构建即死于此（`Invalid option type timestamps`） |

### 3. `triggers { }`

- `gitlab(triggerOnPush: true, triggerOnMergeRequest: false, branchFilterType: 'All', secretToken: '…')`：参数含义、注册机制（**改完必须手动跑一次 job 才写进 config.xml**）、与 webhook 的配合，详见 03 章四.3。
- 原生还有 `cron('H 2 * * *')`、`pollSCM('H/5 * * * *')`（轮询，能配 webhook 就别用轮询）。

### 4. `environment { }`

- 静态键值 + 内置变量（`BUILD_NUMBER`、`WORKSPACE`、`JOB_NAME`、`GIT_BRANCH`…），注入后 agent 里所有 steps、`sh` 都能直接读。
- 引用凭据的写法（本环境未用，备查）：`HARBOR_AUTH = credentials('harbor-push')`（Username with password → 注入 `VAR`/`VAR_USR`/`VAR_PSW`）。

### 5. `stages / stage / steps`

- stage 串行执行；steps 是 DSL 步骤，来源 = Jenkins core + 已装插件。本例 4 个关键步骤：
  - `gitlabCommitStatus(name: 'build') { … }`：块内步骤成功则向 GitLab 报 `build:success`，失败报 `failed`，target_url 自动指向本次构建页（依赖 options 里绑的 connection）。
  - `container('buildctl') { … }`：kubernetes 插件步骤——后续 steps 改在**同 pod 的 buildctl 容器**里 exec（不是 jnlp）。workspace 是 pod 级 emptyDir，所有容器共享，所以 jnlp checkout 的代码 buildctl 直接可见。
  - `sh '…'`：在当前容器里起 bash。
- stage 级还可以 `agent{}`（换节点）、`when{ branch 'main' }`（条件执行）、`parallel`（并行）。

### 6. `sh ''' … '''`（bash 层）

- **三引号**换行原样保留；行尾 `\\` 续行。
- **单引号**是刻意的：阻止 Groovy 插值（`$BUILD_NUMBER` 若被 Groovy 插值会变成构建开始时的快照），让变量在 agent 容器的 bash 里展开——Jenkins 把 environment 块和内置变量以环境变量形式注入容器，bash 自然读到。
- `set -e` 语义：sh 步骤默认非零退出即 step 失败。

### 7. `script { }` 逃生舱 + Groovy 层

需要变量/条件/循环时用 `script { … }` 写 Groovy。kaniko 路线曾用顶层 `def tag = "b${env.BUILD_NUMBER}-${env.GIT_COMMIT?.take(7)}"`（顶层 def 属 scripted 混写，声明式里建议放 `script{}` 或 environment）。

### 8. `post { }`（本环境未用，备查）

```groovy
post {
  success { echo "OK, image b${BUILD_NUMBER} pushed" }
  failure { emailext ... }   // mailer 插件
  always  { archiveArtifacts artifacts: 'report/**', allowEmptyArchive: true }
}
```

### 9. 语法分层总结（看懂任何 Jenkinsfile 的钥匙）

| 层 | 语言 | 例子 |
|---|---|---|
| 声明式 DSL | `pipeline/agent/options/triggers/environment/stages/post` | 结构与调度 |
| 步骤 DSL（插件提供） | `sh/container/gitlabCommitStatus/withCredentials` | 具体动作 |
| bash | sh 块内 | `buildctl …` |
| Groovy | `script{}` / 顶层 def | 逻辑胶水 |

---

## 三、Kubernetes Cloud 与 Pod Template（agent 怎么配）

> 页面入口：`http://jenkins.devops.local:32037/manage/cloud/kubernetes/`。单实例只有一个 cloud：`kubernetes`（chart 自动创建，**不要删**），其下挂 Pod Template。字段值全部取自本环境 `config.xml` 实况。

### 1. Cloud 级字段（Kubernetes Cloud Configuration 页）

连接集群（本环境 in-cluster SA，全免配）：

| 字段 | 当前值 | 释义 |
|---|---|---|
| **Name** | `kubernetes` | cloud 标识。inline podTemplate 默认继承这里 |
| **Kubernetes URL** | `https://kubernetes.default.svc.cluster.local` | apiserver 地址。chart 已配好 SA + ClusterRoleBinding 免凭据直连；跨集群必须显式填 |
| **Kubernetes server certificate** | 空 | 跨集群 + https 才需要 |
| **Disable https certificate check** | 否 | 跨集群连自签 apiserver 时勾（skipTlsVerify） |
| **Kubernetes Namespace** | `jenkins` | **agent pod 建到这个 ns**；RBAC 也只覆盖此 ns，改 ns 要连 RBAC 一起改 |
| **Credentials** | 空 | 本集群走 SA 免密；**跨集群（第二套 cloud）必须填**（Secret text 存对端 SA token） |
| **WebSocket** | 否 | agent 回连方式：否 = JNLP TCP 50000（jenkins-agent svc）；agent 网络访问不到 50000 时才开 |
| **Connection / Read Timeout** | 5 / 15 秒 | 一般不动 |
| **Max requests per host** | 32 | 对 apiserver 并发 HTTP 数，agent 突发时按需调大 |

Jenkins 与 agent 的连接（controller 被连的通道）：

| 字段 | 当前值 | 释义 |
|---|---|---|
| **Jenkins URL** | `http://jenkins.jenkins.svc.cluster.local:8080` | agent pod 回连 controller 的地址。同集群保持 svc 域名；**跨集群必须改成对端可达地址**，否则 agent 起来了也连不回 |
| **Jenkins tunnel** | `jenkins-agent.jenkins.svc.cluster.local:50000` | JNLP 通道。同集群保持 svc |
| **Container Cap** | 10 | 该 cloud 同时存在的容器总数上限（pod × 容器），0=无限 |
| **Pod Labels** | 有（chart 注入） | 附加标签，审计/NetworkPolicy 用 |
| **Defaults Provider Template** | 空（2026-09-29 已清空） | inline podTemplate 未指定字段的取值来源模板。曾悬空引用不存在的 `test` 导致告警，已修复 |

### 2. Pod Template 级字段（Pod Templates → Add）

| 字段 | 当前值（default 模版） | 释义 |
|---|---|---|
| **Name** | `default` | UI 标识。**引用靠 label 不靠 name** |
| **Labels** | `jenkins-jenkins-agent` | pipeline `agent { label 'xxx' }` 匹配的就是它 |
| **Namespace** | `jenkins` | 可覆盖 cloud 级设置 |
| **Usage** | `Normal` | `Only build jobs…` = 仅供显式声明 label 的 job 用 |
| **Pod Retention** | `Never` | build 后 pod 处置：Never 立删 / OnFailure 失败留 / Always 留（排障） |
| **Container Cap** | 空 | 该模版容器上限 |
| **Show raw yaml** | — | 直接看/改生成的 pod yaml，排障利器 |
| **Idle minutes** | 0 | 闲置 N 分钟才删；0 = job 完即删 |
| **Active Deadline Seconds** | 0 | pod 最大存活时长，防僵死 |
| **Service Account** | `default` | agent 无需集群权限（deploy 走 prod-kubeconfig 凭据），保持 default |
| **Node Selector** | 空 | 钉节点。本环境 m1 有 control-plane:NoSchedule 污点而模版无 toleration → agent 实际只落 w1 |
| **Taints/Tolerations** | 空 | 要让 agent 上 m1 才需要 |
| **YAML** | 空 | 整个模版用一段 Pod yaml 定义（与 UI 字段二选一，UI 优先） |
| **Volumes** | （buildkit 模版有） | HostPath / PVC / **Secret Volume（挂凭据最常用）** |
| **Inherited templates** | — | 5.x 特性：从其它模版继承字段组合复用 |

### 3. Container Template 字段（右栏 Add Container）

> jnlp 容器由 chart 预建（`jenkins/inbound-agent`），**command 留空、args `${computer.jnlpmac} ${computer.name}` 是回连握手参数，改了就废**。业务容器（buildctl/maven/kubectl）在其后追加。

| 字段 | 释义 |
|---|---|
| **Name** | 特殊值 `jnlp` = 替换默认 agent 容器；其它名 = 追加 sidecar，pipeline 里 `container('名字')` 进入 |
| **Docker image** | 镜像；要每次构建拉最新勾 always pull |
| **Command / Arguments** | 入口覆盖。long-running sidecar 套路：`sleep` + `9999999`，让容器常驻供 pipeline exec |
| **Working directory** | workspace 路径，同 pod 所有容器共享（emptyDir），默认 `/home/jenkins/agent` |
| **TTY** | 勾上分配 tty，sleep 常驻容器必备（无 tty 的 sleep 会被 SIGHUP 立杀） |
| **Run with privileged** | 特权容器。kaniko/BuildKit rootless 不需要；DinD 必须 |
| **Environment variables** | 注入 env，可引用 Jenkins 凭据 |
| **Resource requests/limits** | jnlp 默认 512m/512Mi；构建容器按需，大镜像建议 cpu 1 / memory 2Gi 起 |
| **Ports / Liveness probe** | sidecar 起服务（dind 2375 等）才需要 |

### 4. 本环境实作模版：`buildkit`

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

⚠️ BuildKit 认证走 gRPC session——buildctl **客户端**把 `~/.docker/config.json` 喂给 daemon，secret 必须挂到 buildctl 容器（`/home/user/.docker`），只挂 buildkitd daemon 侧照样 401。详见 04 章五。

**kaniko 对照（未实施，kaniko 已 EOL，留作方式 B 参考）**：

```text
Name: kaniko-builder
Labels: kaniko
Containers → Add:
  Name: kaniko, Image: gcr.io/kaniko-project/executor:debug
  Command: sleep, Arguments: 9999999, TTY: 勾选, Limits: cpu 1 / memory 2Gi
Volumes: Secret Volume: harbor-push-config → /kaniko/.docker
```

### 5. UI 预定义 vs Jenkinsfile inline（二选一，inline 优先）

```groovy
// 方式 A：inline（job 自带，git 里可追溯）
podTemplate(label: 'kaniko',
    containers: [containerTemplate(name: 'kaniko', image: 'gcr.io/kaniko-project/executor:debug',
        command: 'sleep', args: '9999999', ttyEnabled: true)],
    volumes: [secretVolume(secretName: 'harbor-push-config', mountPath: '/kaniko/.docker')]) {
  node('kaniko') { container('kaniko') { sh '/kaniko/executor ...' } }
}
```

同 label 冲突时以流水线声明的为准。本环境选方式 B（UI/cloud 级预定义 `buildkit`），一次配置所有 job 共用。

### 6. 验证模版能跑

```bash
# 1. 建测试 pipeline（agent 用模版 label）
pipeline {
  agent { label 'jenkins-jenkins-agent' }
  stages { stage('t') { steps { sh 'hostname && echo ok' } } }
}
# 2. 触发同时观察 agent pod（在 devops-w1 起，结束即删）
kubectl --context devops -n jenkins get pods -w
```

---

## 四、凭据体系：Jenkins credentials vs K8s Secret

**判据：谁消费凭据**——jnlp/步骤需要 → Jenkins credentials；构建容器里的工具直接读文件 → K8s Secret 挂文件。

本环境凭据清单：

| 凭据 | 类型 | 存在哪 | 用途 |
|---|---|---|---|
| `gitlab-root-userpass` | Username with password（root / PAT 当密码） | Jenkins credentials | Pipeline from SCM http clone 私仓 |
| `gitlab-api-token` | Secret text（root PAT） | Jenkins credentials | 全局 GitLab Connection 调 API（commit status） |
| `prod-kubeconfig`（未建） | Secret file | Jenkins credentials | 部署段 kubectl（见 04 章九） |
| `harbor-push-config` | K8s Secret（generic，key 必须叫 `config.json`） | ns jenkins | buildctl/kaniko 容器读 `~/.docker/config.json` 推 Harbor |

Jenkins credentials 里注入的写法：

```groovy
withCredentials([usernamePassword(credentialsId: 'gitlab-root-userpass',
    usernameVariable: 'U', passwordVariable: 'P')]) {
  sh 'git clone http://$U:$P@gitlab.devops.local:32037/root/demo-app.git'
}
```

⚠️ secretVolume 的坑：不能用 `create secret docker-registry`（key 是 `.dockerconfigjson`），插件 secretVolume 又不支持 items 重命名——必须 generic + key `config.json`（做法见 04 章五）。

---

## 五、REST 管理（无 UI 自动化）

**心法：UI 只是 REST 的皮**。页面能点的都有 REST 等价物，先逛页面找入口 → F12 Network 看请求 → 换 curl。

| UI 位置 | REST 等价 |
|---|---|
| `/$BUILD_NUMBER/console` 构建日志 | `curl .../job/demo-app/8/consoleText` |
| 构建状态图标 | `.../job/demo-app/8/api/json`（result 字段） |
| 构建历史页 | `.../job/demo-app/api/json?tree=builds[number,result]` |
| `View Configuration` | `.../job/demo-app/config.xml` |
| New Item | `POST /createItem?name=demo-app`（body=config.xml） |
| Build Now | `POST /job/demo-app/build` |
| Manage Jenkins → Script Console | `POST /scriptText`（scriptText 见下） |

- **crumb 机制**：写操作必须带 CSRF crumb，而 crumb 绑定会话——`curl -c /tmp/cj.txt -u admin:$PW .../crumbIssuer` 取 crumb 存 cookie，后续 `-b /tmp/cj.txt -H "$CRUMB"`。
- **scriptText = Groovy 直通活对象**：改配置 = 取对象 → set → `save()` 落盘。建 pod 模版/凭据/连接都用它（完整脚本见 04 章六，凭据与连接见 03 章四）。
- **root URL（JenkinsLocationConfiguration）**：决定一切对外链接（含 commit status 的 target_url）。chart 默认不带端口 → 已改 `http://jenkins.devops.local:32037/`（scriptText 实录见 03 章四.2）。

## 六、构建方式选型

| 方案 | 原理 | privileged | 评价 |
|---|---|---|---|
| 挂宿主 docker.sock | 复用节点 daemon | 不需要（但等同节点 root） | containerd 集群无 sock 可挂；即使有也是最大安全隐患，弃 |
| DinD | pod/sidecar 跑 dockerd，`DOCKER_HOST=tcp://localhost:2375` | **必须要** | 兼容老流水线；特权容器 + 层不共享 + 嵌套 overlayfs 慢，仅救急 |
| kaniko | 用户态逐条执行 Dockerfile 直 push | 不需要 | 无 daemon 无特权，但 **已 EOL**；`--insecure` 兼容无 TLS Harbor，认证走挂载 config.json |
| **BuildKit（本环境）** | 常驻 rootless buildkitd + buildctl 客户端 | rootless 免特权 | 最先进：多阶段并行、`RUN --mount=type=cache/secrets`、registry 缓存导入导出；构建农场部署见 04 章四 |
| nerdctl / kit | containerd 原生构建 | 视模式 | nerdctl 底层仍是 buildkitd；kit（CNCF 沙箱）生态尚早 |

本环境全程不碰 docker CLI：agent pod buildctl → 远程 buildkitd 构建 → 直 push Harbor。
