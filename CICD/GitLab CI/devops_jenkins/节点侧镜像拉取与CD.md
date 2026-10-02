# 节点侧镜像拉取与 CD 部署链路

CI 通了不等于 CD 通。这篇记的是从「push 完镜像」到「Pod 真的跑起来」之间
那段最容易踩空的路：**谁在解析域名、谁在拉镜像、谁有权限改 Deployment**。

配套文件：

| 文件 | 作用 |
|---|---|
| `devops_jenkins/node-registry-fix.yml` | 节点侧修复 DaemonSet：节点 DNS + containerd registry 配置 |
| `devops_jenkins/cd-rbac.yml` | CD 段最小权限（devops 命名空间内改 Deployment / 等 rollout） |
| `devops_jenkins/podtemplate-kubectl.xml` | Jenkins 全局 podTemplate 加一个 kubectl 容器（存档） |
| `test/Jenkinsfile` | 加了 `Deploy` stage 的完整流水线 |

---

## 一、为什么 CI 绿了 CD 还是挂

典型现象两种：

- `kubectl set image` 直接 `Error from server (Forbidden): ... deployments.apps "test" is forbidden`
- set image 成功，但 Pod 卡在 `ImagePullBackOff`，`kubectl describe pod` 里是
  `failed to resolve reference "harbor.devops.local/library/test:vX"` 或 `401 Unauthorized`

根因都不在 Jenkins：

1. **权限**。集群原有的 `ClusterRole jenkins-admin` 只授了 `apiGroups: [""]`
   （core/v1），`set image` 打的是 `apps` 组，落空 → Forbidden。
2. **解析与拉取发生在节点上**。真正拉镜像的是 **kubelet/containerd**，用的是
   **节点的 `/etc/resolv.conf`**，跟 Jenkins、也跟集群内 CoreDNS 都不是一回事。
   CI 里能 `buildctl ... push` 成功，是因为 agent Pod 走的是 CoreDNS（`harbor` 有
   rewrite），完全证明不了节点侧也行。

一句话：**Jenkins 侧和节点侧是两套 DNS、两套拉取路径，要分别打通。**

```
CI  push 路径：  agent Pod  --CoreDNS(rewrite harbor)-->  Service harbor:80  --> Harbor Pod
CD  pull 路径：  节点 kubelet --节点 /etc/resolv.conf-->  harbor.devops.local --> 172.20.57.91
                              ^^^^^^^ 这里才是本篇要修的地方
```

---

## 二、节点侧第一关：私有 DNS

`harbor.devops.local` 不是公网域名，公网 DNS 里没有。节点要能解析它，得有私有解析。

UCloud 的 **UDNS 私有 zone**：

- zone `devops.local`（region `hk-02`，`udnszone-1vyagvpnm67m`），关联到集群所在 VPC
  `uvnet-pzygrvb5`，`Recursion=disable`；
- 记录 `harbor  A  172.20.57.91`，TTL 60，值格式是 `172.20.57.91|1|1`（`值|权重|启用`）；
- **关键**：私有 zone 的解析**不是**由 VPC 默认 resolver（`10.8.255.1/.2`）提供的，
  而是由 UDNS 的**专有解析器 `100.90.90.90` / `100.90.90.100`** 提供。所以节点的
  `resolv.conf` 里必须显式写上这两个 IP，只写 VPC resolver 是解析不到的。

CLI 操作（`ucloud`，profile `lab`，项目 `org-j41zws`）：

```bash
# 建 zone（注意 region 是 zone 级属性，必须显式传）
ucloud udns create --region hk-02 --name devops.local

# 关联 VPC
ucloud udns associate-vpc --region hk-02 --zone-id udnszone-1vyagvpnm67m --vpc-id uvnet-pzygrvb5

# 加记录（值格式 值|权重|启用）
ucloud udns record create --region hk-02 --zone-id udnszone-1vyagvpnm67m \
  --name harbor --type A --value "172.20.57.91|1|1" --ttl 60

# 查记录：flag 是 --zone-id，不是 --dns-zone-id（后者会 unknown flag）
ucloud udns record list --region hk-02 --zone-id udnszone-1vyagvpnm67m
```

> 坑：`ucloud udns list` 不带 `--region` 会用默认 region 查，返回空 `[]`，
> 让人误以为 zone 没建成功。zone 是 region 级的，每个命令都要带对 region。

### resolv.conf 的 systemd-resolved 软链陷阱

改节点 `/etc/resolv.conf` 时最容易犯的错：这个文件在 UK8S 镜像上是**软链**：

```
/etc/resolv.conf -> /run/systemd/resolve/resolv.conf
```

`systemd-resolved`（enabled + active）托管着目标文件。**直接往里写 = 改 resolved 的
动态文件**，网络一变 / 重启就被它重新生成覆盖掉，改动「当时生效、重启失效」。

正规做法（`man resolv.conf` 给的）：**删掉软链，落一个静态文件**。resolved 不会碰
静态文件。校验点 `nsswitch.conf` 是 `hosts: files dns` ⇒ glibc 直接读 `/etc/resolv.conf`，
静态文件对它就是权威来源。

改完要**专门验证耐久性**：`systemctl restart systemd-resolved` 之后再 `cat` 一遍，
还在才算真修好（DaemonSet 日志里的 `DURABLE_OK` 就是查这个）。

---

## 三、节点侧第二关：containerd 信任 http

Harbor 没上 TLS，只走 http。containerd 2.x 的写法是 `/etc/containerd/certs.d/`：

```
/etc/containerd/certs.d/harbor.devops.local/hosts.toml
/etc/containerd/conf.d/20-harbor.toml   # 打开 config_path 的 drop-in
```

`hosts.toml`：

```toml
server = "http://harbor.devops.local"

[host."http://172.20.57.91"]
  capabilities = ["pull", "resolve", "push"]
  skip_verify = true
```

`20-harbor.toml`（UK8S 的主 `config.toml` 是 `version = 3` + `imports = ["/etc/containerd/conf.d/*.toml"]`，
所以 drop-in 会被加载，不用动主文件）：

```toml
version = 3

[plugins.'io.containerd.cri.v1.images'.registry]
  config_path = "/etc/containerd/certs.d"
```

### Harbor 的 token realm 也要能解析

只加 http 例外有时仍然 401，因为 Harbor 返回的挑战头是：

```
Www-Authenticate: Bearer realm="http://harbor.devops.local/service/token",service="harbor-registry"
```

containerd 要**再去解析 realm 里的同一个 host** 才能拿 token。所以 DNS 那一关（第二节）
和这一关必须一起修：只修 certs.d、不修 DNS，realm 解析不了 → 还是 401。

---

## 四、怎么把这些落到三台 worker 上

用一个 `privileged` + `hostPID` 的 DaemonSet，`nsenter -t 1` 进宿主命名空间直接改
（见 `node-registry-fix.yml`）。要点：

- **无 tolerations** ⇒ 只落到 3 台没打污点的 worker（master 有 NoSchedule）。
- **幂等**：写文件前 `cmp` 比对，内容没变就不重启 containerd。**这才是防三台一起抖的关键** ——
  节点配置已落盘后重跑，三台都会打印 `unchanged`，一次 containerd 重启都不会发生。
- **`maxUnavailable: 1` 的实际作用范围**：它只在**滚动更新一个已在运行的 DaemonSet 模板**时
  限制同时下线（删除）的 Pod 数。而本篇的用法是每次巡检完 `delete ds`、下次再 `apply`，
  属于**新建** —— DaemonSet 控制器对新建走 `manage`/`syncNodes`，会**一次性给三台节点各建一个 Pod**，
  不受 `maxUnavailable` 约束。此时如果脚本不是幂等的，三台 containerd 会同时重启。
  所以真正兜底的是上一条 `cmp`，不是 `maxUnavailable`（重启 containerd **不会杀已有容器**，
  只是运行时短暂中断，但仍不该三台一起抖）。
- **日志留存**：脚本跑完 `touch /tmp/done`，readiness 探针检查该文件；主进程
  `exec sleep infinity` 把 Pod 挂着，好收日志，看完 `kubectl delete ds node-registry-fix`
  即可。配置已落盘，需要时再 apply 复现/巡检。

```bash
kubectl apply -f devops_jenkins/node-registry-fix.yml
kubectl -n devops wait --for=condition=Ready pod -l app=node-registry-fix --timeout=300s
# 逐台看日志，应出现 DURABLE_OK 和 PULL_OK(crictl)
kubectl -n devops logs -l app=node-registry-fix | less
kubectl -n devops delete ds node-registry-fix
```

---

## 五、CD 权限

补一个**命名空间级**的最小 Role，绑给已有的 SA `jenkins-admin`
（见 `cd-rbac.yml`）。为什么不直接扩那个 ClusterRole：它已经能跨 namespace 读 secrets，
太宽；CD 只需要在 devops 里改 Deployment，就越权越少越好。

需要两条 `apps` 组权限的原因：`set image` 是 `patch/update deployments`；
`rollout status` 还要**读 ReplicaSet** 才能判断新 RS 是否就绪，所以 replicasets 也要 get/list/watch。

```bash
kubectl apply -f devops_jenkins/cd-rbac.yml
kubectl auth can-i patch  deployments -n devops --as=system:serviceaccount:devops:jenkins-admin   # yes
kubectl auth can-i update deployments -n devops --as=system:serviceaccount:devops:jenkins-admin   # yes
```

---

## 六、Jenkins 侧：podTemplate 加 kubectl 容器

podTemplate `buildkitd-agent` 原本只有 `jnlp` + `buildkitd`，**都没有 kubectl**。
加一个 kubectl 容器（`alpine/k8s` 自带 kubectl）后，Jenkinsfile 里才能
`container('kubectl')` 跑部署。

- pod 级 `serviceAccount: jenkins-admin` ⇒ pod 内所有容器共用这份 SA token，
  kubectl 容器天然继承上面 `cd-rbac.yml` 的权限，不用额外配。
- 该模板 `showRawYaml=true` ⇒ **构建页**能直接看插件合并出来的最终 Pod YAML。
  但想在**全局配置页**核对容器列表，路径跟老教程不一样，见下面「怎么核对 kubectl 容器在不在」。

**落地动作（已执行，2026-10-01）**：Jenkins 是 `HudsonPrivateSecurityRealm` + 本地用户，
密码是 bcrypt，取不回；`/reload` 需要登录。所以走「改盘上文件 + 重启」这条路：

```bash
# 1) 备份 + 把 config.xml 拉出来
kubectl -n devops exec deploy/jenkins -- \
  sh -c 'cp /var/jenkins_home/config.xml /var/jenkins_home/config.xml.bak.$(date +%s)'
kubectl -n devops exec deploy/jenkins -- cat /var/jenkins_home/config.xml > /tmp/jenkins-config-live.xml

# 2) 本地把 kubectl 容器插进 <clouds> 的 <containers> 里，XML 体检通过后写回
kubectl -n devops exec -i deploy/jenkins -- sh -c \
  'cat > /var/jenkins_home/config.xml.new && chmod 644 /var/jenkins_home/config.xml.new \
   && mv /var/jenkins_home/config.xml.new /var/jenkins_home/config.xml' \
  < /tmp/jenkins-config-new.xml
# md5 两侧一致才算写成功

# 3) 重启加载（Recreate 策略，控制器会有几十秒不可用；先确认没有在跑的构建）
kubectl -n devops rollout restart deploy/jenkins
kubectl -n devops rollout status  deploy/jenkins --timeout=300s
```

> 为什么不直接 POST `/config.xml`：需要登录凭据，而本地用户的密码是 bcrypt 不可逆。
> 改盘上文件后重启，Jenkins 启动时按 `config.xml` 重建内存配置，等价。
> 注意**别在 Jenkins 运行期间改完迟迟不重启**——运行中的实例若因 UI/API 操作触发
> `save()`，会把内存态写回盘上、覆盖手改的内容。
>
> 重启后核验：`config.xml` 的 md5 与写入时一致（说明插件接受了、没被规范化掉），
> 且 `curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/login` 返回 200。

### 怎么核对 kubectl 容器在不在

**先说结论：它不是一个 podTemplate，是 `buildkitd-agent` 下面的一个「容器」。**
整份配置里只有一个 podTemplate；找不到一个叫 kubectl 的模板是**正常的**。

而 Kubernetes 插件（本环境 `4557.ve746270f672f`，CloudBees 重写过的那版 UI）把
podTemplate 的 UI **拆成了独立页**，很容易找错地方：

| 你点的位置 | 会看到 | 说明 |
|---|---|---|
| Manage Jenkins → Clouds → `k8s` → **Configure** | 只有 cloud 级字段（Name / Kubernetes URL / Namespace / Credentials…） | 这版 UI 的 `KubernetesCloud/config.jelly` **根本没有容器列表**，看不到是正常的 |
| Manage Jenkins → Clouds → `k8s` → **Pod Templates** | 一张表，只有一列 Name，一行 `buildkitd-agent` | `templates.jelly` 只渲染模板名，**不展开容器** |
| ↑ 再点进 `buildkitd-agent`（或右侧齿轮） | 这一页才有 **Containers** | `PodTemplate/config.jelly` 的 `field="containers"` 在这 |
| Manage Jenkins → **Configure System** | 什么都没有 | 2.580 的 clouds 早不在这页了 |

直达链接（模板 id 从 `config.xml` 的 `<templates><id>` 取）：

```
http://jenkins.devops.local/cloud/k8s/template/<podTemplate 的 id>
```

注意 URL 是 **`/cloud/k8s/...`**，中间的 `cloud` 是 **RootAction 挂载点**，
**不是** `/manage/cloud/...`：

- cloud 列表页 = `/cloud/`（`jenkins.agents.CloudSet.getUrlName()` = `cloud`），
  单个 cloud 页 = `/cloud/k8s/`；
- 侧边栏三个入口是相对链接（`KubernetesCloud/sidepanel.jelly`）：Status = `.`、
  Pod Templates = `templates`、Configure = `configure` ⇒ `/cloud/k8s/templates`、`/cloud/k8s/configure`；
- 模板页链接由 `templates.jelly` 拼出：`getCloudUrl(request2,app,cloud) + "template/" + template.id`
  ⇒ `/cloud/k8s/template/<id>`。

> 该页要 `Jenkins.SYSTEM_READ`（`CloudSet.getTarget()` 里 `checkPermission`），
> 匿名只有 Overall/Read，命中会 403 并跳 `/login?from=...` —— 得登录着看。
> 顺带一句：`/manage/cloud`（Manage Jenkins 里那个「Clouds」条目）是另一个
> `ManagementLink`（`jenkins.agents.CloudsLink`，`urlName` 也是 `cloud`），
> **不是**这些模板页的地址。命令行核对见下，不必翻 UI。

**命令行核对**（比翻 UI 快，且不依赖权限）：

```bash
P=buildkitd-agent-xxxxx   # 构建期间 kubectl -n devops get pod 拿到的 agent pod

# 容器名，一行一个
kubectl -n devops get pod $P -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}'

# 名字 + 镜像
kubectl -n devops get pod $P -o jsonpath='{range .spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}'

# 每个容器 ready / 重启次数
kubectl -n devops get pod $P -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.ready}{"\t"}{.restartCount}{"\n"}{end}'
```

预期三行：`buildkitd`（moby/buildkit:*-rootless）、`kubectl`（alpine/k8s）、`jnlp`
（`jenkins/inbound-agent`，插件自动注入）。

`kubectl get pod` 的 `READY` 列就是容器数：**`3/3` = 3 个声明容器全部就绪**
（不是「3 个 pod」）。`RESTARTS` 是**所有容器重启次数之和**，要看单个容器用上面第 3 条；
`STATUS` 是 Pod 阶段，跟单容器状态不是一回事（某容器 CrashLoopBackOff 时 Pod 仍可能 Running）。

> 三个容器都是 `sleep 9999999` 保活 + `tty:true`，**构建结束 agent pod 即回收**，
> 所以要核对得趁 build 在跑时抓。

---

## 七、验证（跑通的判据）

1. `kubectl auth can-i` 两条都是 yes。
2. 节点侧：DaemonSet 日志出现 `DURABLE_OK` + `PULL_OK(crictl)`。
3. 上真招 —— 连推几个新 tag，每次都是**真拉**：

```bash
# 造一个新 tag 推上去（略），然后
kubectl -n devops set image deployment/test app=harbor.devops.local/library/test:v0.0.5
kubectl -n devops rollout status deployment/test --timeout=180s
kubectl -n devops get pods -l app=test -o wide
```

本环境实测：v0.0.3 / v0.0.4 / v0.0.5 三个连续 tag 都成功 rollout。

4. **完整流水线端到端**（2026-10-01，build #4）：push 到 GitLab → webhook 触发
   `go-pipline` → agent pod 拉起，三个容器齐活：

   ```
   buildkitd <- moby/buildkit:v0.33.0-rootless
   kubectl   <- alpine/k8s:1.34.12
   jnlp      <- jenkins/inbound-agent:...        serviceAccountName: jenkins-admin
   ```

   Deploy 段就在那个 kubectl 容器里跑，构建 `Finished: SUCCESS`：

   ```
   + kubectl -n devops set image deployment/test 'app=harbor.devops.local/library/test:v0.0.4'
   deployment.apps/test image updated
   + kubectl -n devops rollout status deployment/test '--timeout=180s'
   Waiting for deployment "test" rollout to finish: 1 old replicas are pending termination...
   deployment "test" successfully rolled out
   + kubectl -n devops get pods -l 'app=test' -o wide
   test-645cbdf958-rmm7n   1/1   Running   0   66s   10.7.154.29
   ```

> 关键：`go-pipline` 配了 GitLab push trigger（`triggerOnPush=true`），所以
> **推代码即触发**，不必在 UI 里点 Build，也就不受「匿名只读、不能触发构建」的限制。
> 这也解释了为什么改完 Jenkinsfile 必须 push —— 它是从 SCM 读的
> （`CpsScmFlowDefinition`，`scriptPath: Jenkinsfile`，`lightweight: true`），
> 光改工作区里的文件不会生效。

---

## 八、踩坑速查

| 现象 | 真因 | 对策 |
|---|---|---|
| `deployments.apps forbidden` | `jenkins-admin` ClusterRole 只有 core 组 | 加 `cd-rbac.yml` 的 apps 组 Role |
| 节点 `ImagePullBackOff`，解析不到 host | 节点没配私有 DNS | 节点 resolv.conf 指到 `100.90.90.90/.100` |
| 改了 resolv.conf 重启又变回去 | `/etc/resolv.conf` 是 systemd-resolved 的软链 | 删软链、落静态文件；用 `DURABLE_OK` 验耐久 |
| http 例外加了还是 401 | Harbor 的 token realm 也要解析同一 host | DNS 与 certs.d 一起修 |
| `crictl pull` 说 "Image is up to date" 像成功了 | 镜像已缓存，压根没走网络 | 先 `crictl rmi` 删掉再拉，看下载字节数 |
| rollout 很慢 | 本环境 `replicas=1`，Deployment 默认 RollingUpdate 是 25%/25%，取整后 `maxUnavailable=0` ⇒ 一次只换一个 Pod；每个 tag 都是新镜像、每次都要真拉 | 正常，超时给足；要快就调 strategy（注意 DaemonSet 的默认 `maxUnavailable` 是 1，跟 Deployment 不是一回事） |
| `ucloud udns list` 返回空 | 没带 `--region`，zone 是 region 级 | 每条命令都带对 region |
| `ucloud udns record list --dns-zone-id` 报错 | flag 名不对 | 用 `--zone-id` |
| Jenkins UI 里找不到「kubectl 的 podTemplate」 | 它**不是** podTemplate，是 `buildkitd-agent` 下的一个容器；且插件 4557 把模板挪到了独立页 | Clouds → k8s → **Pod Templates** → 点进 `buildkitd-agent`，才看得到 Containers |
| Clouds → `k8s` → **Configure** 页没有容器列表 | 这是 CloudBees 重写后的 UI，cloud 配置页本身不含模板（`KubernetesCloud/config.jelly` 里没有） | 别在 Configure 页找；模板在 `templates` 子页 |
| 云页面 URL 拼成 `/manage/cloud/k8s/...` 打不开 | 模板页在 RootAction `/cloud/k8s/` 下，不在 `/manage/cloud/` 下（那是 `CloudsLink` 这个 ManagementLink 的地址） | 用 `/cloud/k8s/templates`、`/cloud/k8s/template/<id>`（命令行核对见 §六） |
| `/cloud/...` 匿名返回 403（跳 `/login?from=`） | 该页要 `Jenkins.SYSTEM_READ`（`CloudSet.getTarget()` 里 check） | 登录着看（命令行核对见 §六） |

---

## 九、后续

- **已端到端跑通**（2026-10-01，build #4）：kubectl 容器落到 Jenkins podTemplate，
  Jenkinsfile 加了 Deploy 段并 push 到 `main`，webhook 触发 `go-pipline`，
  自动构建 + `set image v0.0.4` + rollout 成功，全绿（证据见第七节第 4 条）。
- 触发方式：`go-pipline` 用 GitLab push trigger，改完 Jenkinsfile 只要 push 就自动跑，
  不用 admin 登录点 Build。
- `sa.yml` 里那个过宽的 ClusterRole（跨 namespace 读 secrets）值得收敛成命名空间级，
  目前只是用新增 Role 补了缺口，没动它。
