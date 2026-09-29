# Jenkins + GitLab + Harbor CI/CD 实战笔记

devops 集群（UCloud HK 裸机 kubeadm）上的完整 CI/CD 流水线：git push → webhook 自动构建（BuildKit rootless）→ 推 Harbor → commit 状态回报 GitLab；prod 部署段配置法已备妥待接入。

## 阅读顺序

| 章 | 文件 | 内容 |
|---|---|---|
| 00 | [cluster-plan.md](cluster-plan.md) | 集群规划背景（devops/prod 双集群、网络、UCloud 云主机） |
| 01 | [01-环境搭建.md](01-环境搭建.md) | Jenkins（Helm）/ GitLab（Omnibus）/ Harbor 三件套安装 + ingress 统一入口 + 22 条踩坑 |
| 02 | [02-Jenkins-Pipeline语法与配置.md](02-Jenkins-Pipeline语法与配置.md) | Pipeline 声明式语法逐段详解、Kubernetes Cloud/PodTemplate、凭据体系、REST 管理、构建方式选型 |
| 03 | [03-GitLab配置.md](03-GitLab配置.md) | GitLab 配置三通道（Omnibus/rails runner/API）、PAT、webhook、Jenkins 集成、API 怪癖速查 |
| 04 | [04-整体流程详解.md](04-整体流程详解.md) | **端到端每一步实操**（CoreDNS→PAT→仓库→Harbor robot→buildkitd→Secret→Jenkins REST→webhook→验证）+ 部署段（prod）+ 故障速查 |

建议先读 01 把环境装起来，做流水线时按 04 章步骤地图逐步执行，语法/配置细节按需回查 02、03。

## 配套文件

| 文件 | 说明 |
|---|---|
| [yaml/jenkins-values.yaml](yaml/jenkins-values.yaml) | Jenkins Helm values（01 章安装用） |
| [yaml/gitlab-omnibus.yaml](yaml/gitlab-omnibus.yaml) | GitLab StatefulSet manifest（含 GITLAB_OMNIBUS_CONFIG 全部启动配置） |
| [yaml/harbor-values.yaml](yaml/harbor-values.yaml) | Harbor Helm values |
| [buildkitd.yaml](buildkitd.yaml) | 常驻 rootless buildkitd 构建农场 manifest（04 章四） |

## 固定值速查（本环境实测值）

| 项 | 值 |
|---|---|
| 统一入口 | `123.58.219.112:32037`（ingress-nginx NodePort，Host 头分流） |
| Jenkins | http://jenkins.devops.local:32037（admin，密码存 Secret `jenkins`） |
| GitLab | http://gitlab.devops.local:32037（root / `Dev0ps#2026`；SSH NodePort 30222） |
| Harbor | http://harbor.devops.local:32037（admin / `Harbor12345`） |
| GitLab 仓库 | `root/demo-app`（project id 2） |
| Harbor 项目 | `demo`（id 3） |
| 流水线 | job `demo-app` → 镜像 `demo/demo-app:b<构建号>` → BuildKit 构建 → webhook 自动触发 |
