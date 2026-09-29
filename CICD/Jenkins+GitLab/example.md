# BuildKit 发布流水线完整示例（Jenkins → GitLab → Harbor）

> 2026-09-29 · 从三件套就绪到构建全绿的**完整实操流程**，所有配置均取自本环境真实值，照抄可复现。
> 姊妹篇：三件套安装《环境搭建.md》· Cloud/模版字段详解《Jenkins-Cloud与PodTemplate配置详解.md》· 设计与踩坑详录《流水线部署步骤.md》
> 实测结果：构建 #3/#4/#5 连绿，产物 `demo/demo-app:b3/b4/b5`（同 digest），#4/#5 命中缓存（`CACHED`）。

## 〇、成果与链路

```
git push → GitLab（webhook 待配，当前手动/REST 触发）
              ↓
Jenkins job demo-app（Pipeline from SCM，lightweight checkout）
  └─ agent pod（cloud 模版 buildkit，ns jenkins，label buildkit）
       ├─ jnlp：回连 controller（chart 预置）
       └─ buildctl 容器（moby/buildkit:v0.33.0-rootless）
            ├─ ~/.docker/config.json ← Secret harbor-push-config   ← 认证源（session）
            └─ buildctl --addr tcp://buildkitd.jenkins.svc:1234 build …
                 └─ buildkitd 常驻（rootless，devops-w1，PVC 20Gi 本地缓存）
                      ├─ FROM：demo/busybox:1.36（先转存进 Harbor）
                      ├─ PUSH：demo/demo-app:b$BUILD_NUMBER（registry.insecure=true）
                      └─ 缓存：导入/导出 demo/cache:demo-app
```

固定值速查（本环境）：

| 项 | 值 |
|---|---|
| 集群/节点 | context `devops`；master devops-m1（内网 10.7.6.245），worker devops-w1；内核 6.8 |
| 统一入口 | `*.devops.local` → EIP 123.58.219.112:32037（ingress-nginx NodePort） |
| GitLab | root / `Dev0ps#2026`，仓库 `root/demo-app`（project id 2） |
| Jenkins | admin 密码见下文命令；REST 需 crumb |
| Harbor | admin / `Harbor12345`，项目 `demo`（project_id 3） |
| 构建 robot | `robot$demo+jenkins-push` / `yacDNbefbXGp0MjEnD1jCAWjmWMESAEt` |
| GitLab PAT | `glpat-REDACTED`（root，api + read/write_repository） |

前置：本机 hosts 已加 `123.58.219.112 jenkins.devops.local gitlab.devops.local harbor.devops.local`，`kubectl --context devops` 可用。三件套未装先看《环境搭建.md》。

```bash
# 就绪自检（全部 200 才继续）
curl -H "Host: jenkins.devops.local" http://123.58.219.112:32037/login -I
curl -H "Host: gitlab.devops.local"  http://123.58.219.112:32037/users/sign_in -I
curl -H "Host: harbor.devops.local"  http://123.58.219.112:32037/api/v2.0/health -I
```

---

## 一、集群内域名解析（CoreDNS hosts 块）

集群内组件（GitLab/Jenkins agent/构建器）都要用 `*.devops.local` 域名，给 devops 集群 CoreDNS 加 hosts（File 有 `reload`，改完约 30s 生效；等不及就重启 pod）。

```bash
# 备份（Corefile 改坏 CoreDNS 全挂）
kubectl --context devops -n kube-system get cm coredns -o yaml > /tmp/coredns-backup.yaml

kubectl --context devops -n kube-system edit cm coredns
# 在 ready 一行之后插入（10.7.6.245 = devops-m1 内网 IP，NodePort 任意节点都通）：
#     hosts {
#        10.7.6.245 jenkins.devops.local gitlab.devops.local harbor.devops.local
#        fallthrough
#     }

kubectl --context devops -n kube-system rollout restart deploy coredns

# 验证（任一 ns 起临时 pod）
kubectl --context devops run dns-check --rm -i --restart=Never --image=busybox:1.36 --command \
  -- sh -c 'nslookup harbor.devops.local && wget -q -O- -T 5 http://harbor.devops.local:32037/api/v2.0/health'
# 期望：Address 10.7.6.245，返回 {"status":"healthy"}
```

> prod 集群要拉 Harbor 镜像时同样做法，hosts IP 换 prod master 内网 IP（本文构建段不涉及 prod）。

---

## 二、GitLab：PAT + 仓库三件套

### 1. 建 PAT

界面（root 登录 → 头像 → Access tokens → Add new token：scopes `api`、`read_repository`、`write_repository`），或 rails：

```bash
kubectl --context devops -n gitlab exec gitlab-0 -c gitlab -- gitlab-rails runner \
  't = User.find_by(username: "root").personal_access_tokens.create!(name: "jenkins-ci", scopes: [:api, :read_repository, :write_repository], expires_at: Date.today + 365); puts t.token'
```

验证：

```bash
curl -H "PRIVATE-TOKEN: glpat-xxx" -H "Host: gitlab.devops.local" \
  http://123.58.219.112:32037/api/v4/user    # 返回 root 的 JSON
```

### 2. 建项目 demo-app（Private），提交三个文件

界面建 `root/demo-app`，文件用界面加或 API 推（API 时 `project id = 2`）：

```bash
GL=http://123.58.219.112:32037; H='Host: gitlab.devops.local'; PT='PRIVATE-TOKEN: glpat-xxx'
# 逐个文件：POST /api/v4/projects/2/repository/files/<文件名> ，body {"branch":"main","content":...,"commit_message":...}
```

**Jenkinsfile**（仓库根目录）：

```groovy
pipeline {
  agent { label 'buildkit' }
  options { disableConcurrentBuilds() }   // timestamper 插件没装就不要写 timestamps()
  environment {
    HARBOR   = 'harbor.devops.local:32037'
    PROJECT  = 'demo'
    APP      = 'demo-app'
    BUILDKIT = 'tcp://buildkitd.jenkins.svc.cluster.local:1234'
  }
  stages {
    stage('Build & Push (BuildKit)') {
      steps {
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
```

**Dockerfile**（FROM 用 Harbor 转存镜像，绕开 docker.io 香港超时；转存见第三节）：

```dockerfile
FROM harbor.devops.local:32037/demo/busybox:1.36
COPY index.html /www/index.html
EXPOSE 8080
CMD ["httpd","-f","-p","8080","-h","/www"]
```

**index.html**：

```html
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>demo-app</title></head>
<body><h1>demo-app v1 (BuildKit pipeline)</h1></body></html>
```

> 坑：Jenkins agent jnlp checkout 后 workspace 是 emptyDir，同 pod 的 buildctl 容器直接读（`--local context=$WORKSPACE`），**免二次 clone**——比 kaniko 的 `--context=git://` 优雅。

---

## 三、Harbor：项目 + robot + 基础镜像转存

### 1. 建项目

```bash
curl -u admin:Harbor12345 -H "Host: harbor.devops.local" -X POST \
  http://123.58.219.112:32037/api/v2.0/projects -H "Content-Type: application/json" \
  -d '{"project_name":"demo","metadata":{"public":"false"}}'      # 201，project_id=3
```

### 2. 建 robot（⚠️ Harbor 2.15 只剩系统级端点）

```bash
curl -u admin:Harbor12345 -H "Host: harbor.devops.local" -X POST \
  -H "Content-Type: application/json" \
  -d '{"name":"jenkins-push","duration":-1,"level":"project","permissions":[{"access":[{"action":"pull","resource":"repository"},{"action":"push","resource":"repository"}],"kind":"project","namespace":"demo"}]}' \
  http://123.58.219.112:32037/api/v2.0/robots
# 201 → {"name":"robot$demo+jenkins-push","secret":"yacDN...","id":1}   secret 只显示这一次！
```

> 坑（全踩过）：
> - 项目级端点 `POST /api/v2.0/projects/demo/robots` 已删（404）；必须系统级 `/api/v2.0/robots` + `level:"project"` + `permissions[].namespace`
> - 用户名格式是 **`robot$项目名+robot名`**（`robot$demo+jenkins-push`），不是老的 `demo+jenkins-push`
> - `duration` 缺省报 400；`GET /api/v2.0/robots` 列表返回空（quirk），按 id 查单个：`GET /api/v2.0/robots/1`

### 3. 转存基础镜像（香港拉 docker.io 不稳，全部走 Harbor）

```bash
skopeo copy --dest-tls-verify=false \
  --dest-creds 'robot$demo+jenkins-push:<secret>' \
  docker://busybox:1.36 docker://harbor.devops.local:32037/demo/busybox:1.36
```

---

## 四、构建农场：常驻 rootless buildkitd

manifest 即本目录 `buildkitd.yaml`，要点（rootless 三要素 + 两个专属坑）：

| 项 | 值 | 原因 |
|---|---|---|
| image | `moby/buildkit:v0.33.0-rootless`（UID 1000） | rootless 要素① |
| `seccompProfile: Unconfined` | + `--oci-worker-no-process-sandbox` | rootless 要素②③ |
| AppArmor 注解 `unconfined` | 容器器级 annotation | **containerd 2.x 默认 profile 禁 mount → rootlesskit `failed to share mount point /: permission denied`**，最大的坑 |
| initContainer `chown -R 1000:1000 /cache` | runAsUser 0 | local-path PVC 目录 root 属主，rootless 写不了 |
| buildkitd.toml `[registry."harbor…:32037"] http=true` | ConfigMap | daemon 拉取明文（Harbor 无 TLS） |
| nodeSelector devops-w1 + PVC 20Gi | local PV 绑节点 | 与 Harbor 同机 |
| readinessProbe `buildctl … debug workers` | — | 探活真实有效 |

```bash
kubectl --context devops apply -f buildkitd.yaml
kubectl --context devops -n jenkins rollout status deploy/buildkitd
# 验证 worker：应看到 overlayfs snapshotter
kubectl --context devops -n jenkins exec deploy/buildkitd -c buildkitd -- \
  buildctl --addr tcp://127.0.0.1:1234 debug workers
```

> 内核 6.8 → rootless overlayfs 原生可用，无需 fuse。

---

## 五、认证 Secret（一份内容，挂两个位置）

**先记结论（本流程核心坑）**：BuildKit 认证走 **gRPC session**——buildctl **客户端**把自己 `~/.docker/config.json` 里的凭据喂给 daemon（拉 FROM、push 输出全算）。daemon 单独挂 config.json 不能让远程构建通过认证 → **Secret 必须挂到 agent pod 的 buildctl 容器**（本地 daemon 那份只是保险）。

种 Secret（⚠️ key 必须叫 `config.json`；不能用 `create secret docker-registry`——它的 key 是 `.dockerconfigjson`，插件 secretVolume 改不了名）：

```bash
AUTH=$(printf 'robot$demo+jenkins-push:yacDNbefbXGp0MjEnD1jCAWjmWMESAEt' | base64)
cat > /tmp/config.json <<EOF
{"auths":{"harbor.devops.local:32037":{"username":"robot\$demo+jenkins-push","password":"yacDNbefbXGp0MjEnD1jCAWjmWMESAEt","auth":"$AUTH"}}}
EOF
kubectl --context devops -n jenkins create secret generic harbor-push-config \
  --from-file=config.json=/tmp/config.json
rm -f /tmp/config.json
```

> 坑：heredoc 未加引号时 `$demo` 会被 shell 展开吃掉用户名，务必像上面这样 `robot\$demo` 转义。

排障手法：怀疑认证时先 exec 进 buildkitd pod 用 **本地** buildctl 跑通拉+推（`--output type=image,…,push=true`），即可证明 daemon 侧没毛病，锁死客户端侧没挂 secret。

---

## 六、Jenkins 配置（全 REST，无 UI 操作）

Jenkins 2.176+ crumb 与会话绑定：先 `curl -c cj.txt` 取 crumb，后续 POST 带 `-b cj.txt` + crumb 头。

```bash
PW=$(kubectl --context devops -n jenkins exec svc/jenkins -c jenkins -- cat /run/secrets/additional/chart-admin-password)
J=http://jenkins.devops.local:32037
CRUMB=$(curl -s -c /tmp/cj.txt -u "admin:$PW" "$J/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,\":\",//crumb)")
```

### 1. 凭据（gitlab-root-userpass：root/PAT，http clone 私仓用）

```bash
cat > /tmp/cred.xml <<'EOF'
<com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl>
  <scope>Global</scope>
  <id>gitlab-root-userpass</id>
  <username>root</username>
  <password>glpat-xxx</password>
  <description>root PAT (http clone)</description>
</com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl>
EOF
curl -s -u "admin:$PW" -b /tmp/cj.txt -H "$CRUMB" \
  --data-urlencode "credentials=$(cat /tmp/cred.xml)" \
  "$J/credentials/store/system/domain/_/createCredentials"
```

### 2. 建 agent pod 模版（标签 `buildkit`）

`POST /scriptText` 跑 Groovy。⚠️ 两个易错点：`SecretVolume` 构造参数是 **(mountPath, secretName)**（传反了挂的是名为路径的 secret）；`PodTemplate` 没有 `setWorkingDir`（只有容器级有）。

```groovy
import jenkins.model.Jenkins
import org.csanchez.jenkins.plugins.kubernetes.*
import org.csanchez.jenkins.plugins.kubernetes.volumes.SecretVolume

def j = Jenkins.get()
def cloud = j.clouds.find { it.name == 'kubernetes' }
cloud.getTemplates().removeAll { it.getName() == 'buildkit' }
def c = new ContainerTemplate('buildctl', 'moby/buildkit:v0.33.0-rootless')
c.setCommand('sleep'); c.setArgs('9999999'); c.setTtyEnabled(true)
c.setWorkingDir('/home/jenkins/agent')
def t = new PodTemplate()
t.setName('buildkit'); t.setLabel('buildkit'); t.setNamespace('jenkins')
t.setContainers([c])
t.setVolumes([new SecretVolume('/home/user/.docker', 'harbor-push-config')])
cloud.addTemplate(t)
cloud.setDefaultsProviderTemplate(null)   // 清掉悬空引用（chart 曾带 test）
j.save()
return 'OK'
```

```bash
curl -s -u "admin:$PW" -b /tmp/cj.txt -H "$CRUMB" \
  --data-urlencode "script=$(cat /tmp/podtemplate.groovy)" "$J/scriptText"
# 验证（重点看 volumes 的 mountPath/secretName 没颠倒）：
#   脚本里 return t.getVolumes().toString() 应出现 mountPath=/home/user/.docker, secretName=harbor-push-config
```

同步检查 cloud 级：Manage Jenkins → Clouds，**Defaults Provider Template 必须为空**（悬空引用会告警并影响 inline 模版兜底）。

### 3. 建 job demo-app（Pipeline from SCM）

```bash
cat > /tmp/job.xml <<'EOF'
<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>http://gitlab.devops.local:32037/root/demo-app.git</url>
          <credentialsId>gitlab-root-userpass</credentialsId>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
      <branches><hudson.plugins.git.BranchSpec><name>*/main</name></hudson.plugins.git.BranchSpec></branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="empty-list"/>
      <extensions/>
    </scm>
    <scriptPath>Jenkinsfile</scriptPath>
    <lightweight>true</lightweight>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
EOF
curl -s -u "admin:$PW" -b /tmp/cj.txt -H "$CRUMB" \
  --data-binary @/tmp/job.xml -H "Content-Type: application/xml" \
  "$J/createItem?name=demo-app"
```

> job 名必须与将来 webhook URL 路径一致（`/project/demo-app`）。Harbor 认证不进 Jenkins 凭据（走 K8s Secret + session）。

---

## 七、触发构建 + 端到端验证

```bash
# 触发
curl -s -o /dev/null -w "%{http_code}\n" -X POST -u "admin:$PW" -b /tmp/cj.txt -H "$CRUMB" "$J/job/demo-app/build"   # 201

# 轮询（build 编号在开始执行时才出现，Poll 要直接按编号查）
curl -s -u "admin:$PW" "$J/job/demo-app/1/api/json?tree=building,result"

# 看日志
curl -s -u "admin:$PW" "$J/job/demo-app/1/consoleText"
```

**一次通过的全绿日志要点**（build #3 实录）：

```
#1 [internal] load build definition from Dockerfile
#5 [internal] load metadata for harbor.devops.local:32037/demo/busybox:1.36
#12 [auth] demo/demo-app:pull,push token for harbor.devops.local:32037    ← session 认证通
#6 importing manifest list ...                                            ← 从 demo/cache:demo-app 导入缓存
#14 exporting layers ... pushing manifest ...                             ← push 明文 OK
```

**Harbor 验收：**

```bash
curl -s -u admin:Harbor12345 -H "Host: harbor.devops.local" \
  'http://123.58.219.112:32037/api/v2.0/projects/demo/repositories/demo-app/artifacts?page_size=10' \
  | python3 -m json.tool   # b3/b4/b5 挂同一 digest；再跑一次构建应看到 CACHED
```

本环境 5 次构建实录（排障过程本身就是教材）：

| 构建 | 结果 | 教训 |
|---|---|---|
| #1 | ❌ `Invalid option type timestamps` | timestamper 插件没装，options 别写 timestamps() |
| #2 | ❌ `FROM … 401 Unauthorized` | secret 只挂了 daemon、没挂 buildctl 容器（§五 结论） |
| #3 | ✅ push b3 | session 认证通（`[auth] … token`） |
| #4 | ✅ push b4，`#9 CACHED` | registry 缓存命中 |
| #5 | ✅ push b5（验证代理复核跑的） | 可复现性独立确认 |

## 八、故障速查（本流程踩过的全在这）

| 症状 | 原因 → 修法 |
|---|---|
| buildkitd 崩溃：rootlesskit `failed to share mount point /: permission denied` | containerd 2.x 默认 AppArmor 禁 mount → pod 注解 `container.apparmor.security.beta.kubernetes.io/buildkitd: unconfined` |
| 构建里 `FROM/push 401` | BuildKit session 认证凭据在客户端：把 harbor-push-config 挂到 agent 模版 buildctl 容器 `/home/user/.docker` |
| push 报 TLS/https 错 | `--output` 里加 `registry.insecure=true`（buildkitd.toml 的 `http=true` 只管 daemon 拉取） |
| FROM docker.io 超时 | 香港网络：`skopeo copy` 转存到 Harbor，FROM 改指 `harbor…:32037/demo/busybox:1.36` |
| buildkitd PVC 写不进 | local-path 目录 root 属主 → initContainer `chown -R 1000:1000` |
| secretVolume 挂载名/路径颠倒 | 构造参数顺序就是 `(mountPath, secretName)`；挂完 Groovy `getVolumes().toString()` 验 |
| kaniko 类方案 `unauthorized` 且密码没错 | Secret key 不是 `config.json`；或用户名没带 `robot$demo+` 前缀 |
| Jenkins REST 403 No valid crumb | crumb 绑定会话：`curl -c` 存 cookie 再 `-b` 带上 |

## 九、尚未实施（要补的路）

- **GitLab webhook 自动触发**：demo-app → Settings → Webhooks → `http://jenkins.devops.local:32037/project/demo-app`（Push events）+ Jenkins job 勾 GitLab trigger（见《流水线部署步骤.md》第五步.5）
- **部署段（prod）**：`prod-kubeconfig` 凭据 + `set image` 滚动发布 + prod 节点 containerd http 拉取配置（见《流水线部署步骤.md》第四步）
- Harbor Trivy 扫描、tag 规则、垃圾回收
