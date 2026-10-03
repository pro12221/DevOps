# devops_gitlab_runner —— 自建 GitLab Runner Helm chart

不用官方 `gitlab/gitlab-runner` chart，自己写一个最小可用版，把 GitLab Runner
以 **kubernetes executor** 部署到 UK8S 集群的 `devops` namespace。

目标环境（当前 POC）：GitLab CE 18.11.12 @ `http://gitlab.devops.local`、
Harbor @ `harbor.devops.local`（http，无 TLS）、rootless `buildkitd` 常驻在
`tcp://buildkitd:1234`。

---

## 1. 先想清楚 runner 在 k8s 上到底怎么工作

kubernetes executor 的模型是 **runner 本体不干活**：

```
GitLab ──(long polling 拉 job)──> runner Pod（常驻，1 个副本）
                                     │  用 k8s API 动态创建
                                     ▼
                                   job Pod（每个 pipeline job 一个）
                                     ├─ helper 容器：clone 代码、传产物
                                     └─ build 容器：跑 .gitlab-ci.yml 的 script
```

由此推出 3 个硬性要求，也是这个 chart 的骨架：

1. runner 需要**权限**去创建/删除/exec job Pod → 一个 SA + namespace 级 Role。
2. runner 需要**一份 config.toml**，告诉它 GitLab 地址、token、executor 类型、
   job Pod 的镜像/资源/挂载。
3. job Pod 的**运行参数**全写在 config.toml 的 `[runners.kubernetes]` 段里，
   不是写在 Deployment 里 —— 这点最容易搞混。

所以 chart 里真正有内容的只有三个模板：`secret-config.yaml`（渲染 config.toml）、
`deployment.yaml`（跑 runner 进程）、`rbac.yaml`（授权）。

---

## 2. 目录结构

```
devops_gitlab_runner/
├── Chart.yaml                        # chart 元信息，appVersion 跟着 runner 镜像走
├── values.yaml                       # 所有可调项，每个字段都写了用途
├── .helmignore
├── README.md
└── templates/
    ├── _helpers.tpl                  # 名字/标签/引用名（selectorLabels 与 labels 分开）
    ├── serviceaccount.yaml           # runner 用的 SA
    ├── rbac.yaml                     # Role + RoleBinding：pods/exec/log/secret/cm/service
    ├── secret-config.yaml            # ★ 渲染出整份 config.toml，放进 Secret
    ├── deployment.yaml               # ★ runner 进程 + initContainer 拷 config
    ├── service.yaml                  # 仅 session server 开启时才建
    └── NOTES.txt                     # 安装后提示（含「token 为空」告警）
```

---

## 3. 几个关键取舍（为什么这么写）

### 3.1 不用 `register`，直接写 authentication token

GitLab 16.0 引入 authentication token（`glrt-` 开头），**18.0 起传统
registration token 及 `POST /api/v4/runners` 注册接口已移除**。本集群是
GitLab 18.11，所以：

- 不能再用「`gitlab-runner register --registration-token ...` 生成 config.toml」那套；
- 流程变成：**先在 GitLab 建 runner 拿 `glrt-` token → 把 token 写进 config.toml
  → runner 直接 `run`**。

好处：不需要 initContainer 里塞一个「注册 Job」，幂等、无状态，重装/扩副本都不会重复注册。

### 3.2 config.toml 放 Secret，而不是 ConfigMap

config.toml 里含 `token = "glrt-..."`。放进 ConfigMap 等于
`kubectl get cm -o yaml` 就能明文看到 token。所以整份文件进 Secret。

### 3.3 Secret 只读 → initContainer 拷到 emptyDir

Secret 卷是只读的，而 runner 运行时要往配置目录写 `.runner_system_id`、
runner 状态等文件。做法：

- initContainer `cp /config-src/config.toml /config/config.toml` 拷进一块 emptyDir；
- runner 用 `--config /etc/gitlab-runner/config.toml`，配置目录可写。

（更省事的替代是 subPath 只挂单个文件，代价是每次启动打一条「写入失败」warning。）

### 3.4 `privileged=false`，镜像构建交给常驻 buildkitd

集群里已有 rootless `buildkitd`（Service `buildkitd:1234`，Jenkins 也在用它）。
job 里跑 `buildctl --addr tcp://buildkitd:1234` 就能构建+推送镜像，
**不需要 privileged、不需要 DinD、不需要改 Pod Security Admission**。

`buildctl` 的推送凭据取自客户端 `~/.docker/config.json`；rootless buildkit 镜像
`HOME=/home/user`，所以把已有的 `harbor-push-config` secret 挂到
`/home/user/.docker`（见 `values.yaml` 的 `kubernetes.secretVolumes`）。

### 3.5 安全上下文用 uid 999，不是 100

`docker inspect` 出来的实际值：镜像里 `gitlab-runner` 用户是 **uid/gid 999**，
`/home/gitlab-runner` 属主也是 999。默认 `podSecurityContext` 就按 999 配，
避免 `runAsNonRoot` 下写不了 home 目录。

---

## 4. 前置条件

- 集群能解析并访问 `gitlab.devops.local`（本 POC 里 GitLab 就在同集群 `devops` ns）；
- `devops` ns 下存在 `harbor-push-config`（构建推送镜像用）。换 namespace 要重建同名 secret；
- 有 `buildkitd` Service（要跑镜像构建时）。

```bash
kubectl -n devops get secret harbor-push-config   # 应该存在，key=config.json
kubectl -n devops get svc buildkitd
```

---

## 5. 拿 runner token（`glrt-`）

**方式 A：控制台**

`http://gitlab.devops.local/admin/runners` → 右上 **New instance runner** →
平台选 Linux、勾/填 tag（要和 `.gitlab-ci.yml` 的 `tags:` 对上，本仓库是
`go-pipline`）→ 创建后页面会显示一次 `glrt-xxxxxxxx`，**只显示一次，复制走**。

**方式 B：API**（需要一个 `create_runner` 权限的 PAT）

```bash
curl -sS --request POST \
  --url "http://gitlab.devops.local/api/v4/user/runners" \
  --header "PRIVATE-TOKEN: <你的 PAT>" \
  --data "runner_type=instance_type" \
  --data "description=k8s-runner" \
  --data "tag_list=go-pipline"
# 返回 JSON 里的 "token" 字段即 glrt- 开头的 token
```

---

## 6. 安装

```bash
cd "CICD/GitLab CI/devops_gitlab_runner"

helm upgrade --install gitlab-runner . -n devops \
  --set runner.token=glrt-xxxxxxxxxxxxxxxx
```

不想把 token 打到 shell history 里，就写一个**不提交**的 values 文件：

```bash
cat > values.local.yaml <<'EOF'
runner:
  token: glrt-xxxxxxxxxxxxxxxx
EOF
helm upgrade --install gitlab-runner . -n devops -f values.local.yaml
```

> `.helmignore` 已排除 `values.local.yaml` 与 `values-*.local.yaml`，不会被打进 chart 包。

常用覆盖项：

| 想改什么 | 怎么传 |
|---|---|
| job 接哪些 tag | `--set runner.tags=go-pipline,build` |
| job 默认镜像 | `--set kubernetes.image=alpine:3.21` |
| 让 CI 能 kubectl 做 CD | `--set kubernetes.serviceAccount=jenkins-admin` |
| 拉私有基础镜像 | `--set 'kubernetes.imagePullSecrets[0]=harbor-pull'` |
| helper 镜像走内网 Harbor | `--set kubernetes.helperImage=harbor.devops.local/library/gitlab-runner-helper:x86_64-v18.11.4` |
| 开 web terminal | `--set sessionServer.enabled=true --set sessionServer.serviceType=NodePort --set sessionServer.nodePort=30093 --set sessionServer.advertiseAddress=NODE_IP:30093`（NODE_IP 换成集群外可达的节点 IP；`advertiseAddress` 不设时 runner 向 GitLab 上报 `0.0.0.0`，浏览器连不上终端） |

---

## 7. 验证

```bash
# 1) Pod 起来了
kubectl -n devops get deploy,pod -l app.kubernetes.io/name=gitlab-runner

# 2) 日志里出现 Configuration loaded，且没有 token/401 报错
kubectl -n devops logs deploy/gitlab-runner -f
#   正常：「Configuration loaded」「Initializing executor providers」
#   异常：「Checking for jobs... failed ... 401」「invalid token」

# 3) GitLab 侧能看到 runner 在线
#   项目 Settings → CI/CD → Runners，或 http://gitlab.devops.local/admin/runners
```

跑一个本地测试 pipeline（`.gitlab-ci.yml` 里 `tags` 必须命中）：

```yaml
stages: [test]
hello:
  stage: test
  tags: [go-pipline]
  image: alpine:3.21
  script:
    - echo "runner 接单成功 from $CI_RUNNER_DESCRIPTION"
```

`kubectl -n devops get pods -w` 能看到 job Pod 被拉起来又销毁。

---

## 8. 用 buildkitd 构建并推送镜像

job 走无特权路线，复用集群里常驻的 rootless buildkitd：

```yaml
build-image:
  stage: build
  tags: [go-pipline]
  image:
    name: moby/buildkit:v0.33.0-rootless
    entrypoint: [""]          # 覆盖镜像默认 entrypoint(buildkitd)，只当客户端用
  script:
    - buildctl --addr tcp://buildkitd:1234 build
        --frontend dockerfile.v0
        --local context=. --local dockerfile=.
        --output type=image,name=harbor.devops.local/library/test:$CI_COMMIT_SHORT_SHA,push=true
```

push 凭据来自本 chart 挂进 job Pod 的 `/home/user/.docker/config.json`
（内容就是 `harbor-push-config`），无需在 CI 变量里再放 Harbor 密码。

---

## 9. 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| job 一直 pending，提示 "job is stuck" | `.gitlab-ci.yml` 的 `tags` 和 `runner.tags` 不匹配；或 `run_untagged=false` 而 job 没 tag |
| 日志刷 `Checking for jobs... failed status=401` | token 不对/已 revoke/不是 `glrt-`；重新建 runner 拿新 token 再 `helm upgrade --set runner.token=...` |
| job Pod 建不起来，报 secret 不存在 | `volumes.secret` 没有 optional 语义；换 namespace 后要建同名 `harbor-push-config`，或删掉 `kubernetes.secretVolumes` |
| helper 镜像拉不动（Docker Hub 慢） | 设 `kubernetes.helperImage` 指向 Harbor 里的 helper 镜像 |
| namespace 开了 PSA restricted | 给 `kubernetes.podSecurityContext` 设 `{runAsNonRoot: true, runAsUser: 1000, fsGroup: 1000, seccompProfile: {type: RuntimeDefault}}`（注意 buildkit/kaniko 镜像自身是 uid 1000） |
| 想看 job 为什么失败 | runner 日志 `logLevel=debug`；或 `kubectl -n devops get pod` 找那个 job Pod 看 describe/logs |

---

## 10. 卸载

```bash
helm uninstall gitlab-runner -n devops
# 顺手去 GitLab 把对应 runner 删掉（Admin → CI/CD → Runners）
```
