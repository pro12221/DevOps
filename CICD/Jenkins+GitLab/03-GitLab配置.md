# 03 GitLab 配置（认证、集成与 API）

> 2026-09-30 · 本章管"装好之后为 CI/CD 做的所有 GitLab 配置"——配置入口模型 / PAT / webhook / Jenkins 集成三通道 / API 怪癖
> 安装与 Omnibus 启动配置（external_url、listen_port、monitoring_whitelist 等）见《01-环境搭建.md》第二章
> 章节导航：[01-环境搭建.md] → [02-Jenkins-Pipeline语法与配置.md] → **本章** → [04-整体流程详解.md]

---

## 一、配置入口模型（先懂在哪改，再谈改什么）

本环境 GitLab 是 Omnibus 单容器 + **`/etc/gitlab` 是 emptyDir**，由此定死三条改法通道：

| 通道 | 改什么 | 生效方式 | 坑 |
|---|---|---|---|
| ① manifest `GITLAB_OMNIBUS_CONFIG`（yaml/gitlab-omnibus.yaml） | 启动期配置：external_url、监听端口、探针白名单、puma/sidekiq 资源 | 改完 apply + **手动删 pod** 才滚动 | gitlab.rb 不落盘，exec 里改必失忆 |
| ② `gitlab-rails runner`（kubectl exec 进容器） | 存 DB 的应用设置、用户、token | 即时进 DB | 加密列可能解不开（见二.3）；改完要清 Redis 缓存 |
| ③ REST API（PAT 调 /api/v4） | 同 ② 的 DB 设置 | 即时 | 走属性加密路径，本环境会 500（同 ② 根因） |

**判据**：启动行为（端口/域名/资源）走 ①；运营态设置（允许内网 webhook、改密码、发 token）走 ②③。

## 二、rails runner 改设置：两个必踩的坑

### 1. OpenSSL::CipherError（为什么 API/rails 正常写法全炸）

根因链：`/etc/gitlab` 是 emptyDir → pod 每次重建 `gitlab-secrets.json` 重新生成 → DB 里 ciphertext 属性（加密的 settings 列）用新密钥解不开 → 任何走属性加密的写法（API PUT、rails `update!`/`save!`）炸 `OpenSSL::CipherError`。

**绕法**：`update_columns` 直写 DB（跳过校验与加密路径）。

### 2. Redis 缓存旧副本（"改完读回来还是 false"）

`ApplicationSetting.current` 有 `application_setting:current` 的 Redis 缓存（marshal 副本）。`update_columns` 只写 DB 不清缓存，读回还是旧值——**极易误判"没生效"**。删了这个缓存键再读。

### 3. 实作样例：放行 local webhook（内网回调）

GitLab 10.6+ 默认禁止 webhook 打内网地址，而 Jenkins 与 GitLab 同集群，必须放行：

```bash
# UI 路径（也可）：Admin → Settings → Network → Outbound requests
#   → Allow requests to the local network from webhooks
# PUT /application/settings → 500；rails update!/save! → OpenSSL::CipherError
# 只能 update_columns 直写 + 删 Redis 缓存：
kubectl --context devops -n gitlab exec gitlab-0 -c gitlab -- gitlab-rails runner '
  ApplicationSetting.current.update_columns(allow_local_requests_from_web_hooks_and_services: true)
  Rails.cache.delete("application_setting:current") rescue nil
  puts ApplicationSetting.current.allow_local_requests_from_web_hooks_and_services'
# → true
```

### 4. root 密码已知化（装机后第一件事）

初始密码存 emptyDir 里的 `/etc/gitlab/initial_root_password`，pod 重建即丢——首次启动后立刻改：

```bash
kubectl --context devops -n gitlab exec gitlab-0 -c gitlab -- gitlab-rails runner \
  'u = User.find_by(username: "root"); u.password = "Dev0ps#2026"; u.password_confirmation = "Dev0ps#2026"; u.save!'
```

> 密码不能含 "gitlab" 等常用词（强度校验拒收 `Gitlab@2026`）；验证密码别用 Basic Auth 试（见下），用 `User#valid_password?` 或 Web 登录。

## 三、认证模型：PAT 是唯一 API 凭据

**GitLab 16.0+ 移除了 API 的密码 Basic Auth**：`curl -u root:pass /api/v4/...` 一律 401（正确密码也 401），不是密码错也不是网络故障。密码只剩 Web 登录用；**API/CI 集成必须 PAT**。

### 1. 建 PAT（root，名 `jenkins-ci`，实测 `glpat-REDACTED`）

界面：root 登录 → 头像 → Edit profile → Access tokens → Add new token（Name `jenkins-ci`，Expiration 1 年，Scopes `api` / `read_repository` / `write_repository`）。

或 rails（PAT 必须带 `expires_at`，nil 被校验拒绝）：

```bash
kubectl --context devops -n gitlab exec gitlab-0 -c gitlab -- gitlab-rails runner \
  't = User.find_by(username: "root").personal_access_tokens.create!(name: "jenkins-ci", scopes: [:api, :read_repository, :write_repository], expires_at: Date.today + 365); puts t.token'
```

```bash
# 验证（返回 root 的 JSON 即通）
curl -H "PRIVATE-TOKEN: glpat-xxx" -H "Host: gitlab.devops.local" \
  http://123.58.219.112:32037/api/v4/user
```

> PAT 只显示一次，存好；走本机要带 `Host` 头（ingress 按 Host 分流，见 01 章〇）。

### 2. 本环境 PAT 的两处消费

| 消费点 | 存哪 | 形态 |
|---|---|---|
| Pipeline from SCM 拉 Jenkinsfile | Jenkins 凭据 `gitlab-root-userpass`（Username with password） | 密码字段填 PAT（当密码用，http clone 认证） |
| gitlab-plugin 调 API（commit status） | Jenkins 凭据 `gitlab-api-token`（Secret text） | 走 GitLab Connection，见下 |

## 四、与 Jenkins 集成的三条通道

### 1. 代码通道：`gitlab-root-userpass` 凭据

job 定义 `Pipeline script from SCM`（`CpsScmFlowDefinition`）时 userRemoteConfigs 里引用它 clone 私仓 `root/demo-app`（id 2）。

### 2. API 通道：全局 GitLab Connection

`gitlabCommitStatus` 回报状态靠它调 GitLab API。名字 `gitlab` 必须与 Jenkinsfile `gitLabConnection('gitlab')` 一致。REST createCredentials 在本环境 400（"This page expects a form submission"），全部用 scriptText 直插（crumb 心法见 02 章五）：

**建 PAT 凭据 `gitlab-api-token`：**

```groovy
// POST /scriptText
import com.cloudbees.plugins.credentials.domains.Domain
import com.cloudbees.plugins.credentials.SystemCredentialsProvider
import com.cloudbees.plugins.credentials.CredentialsScope
import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl
import hudson.util.Secret
def store = SystemCredentialsProvider.getInstance().getStore()
if (store.getCredentials(Domain.global()).findAll { it.id == 'gitlab-api-token' }.isEmpty()) {
  store.addCredentials(Domain.global(), new StringCredentialsImpl(CredentialsScope.GLOBAL,
    'gitlab-api-token', 'GitLab root PAT (api)', Secret.fromString('glpat-REDACTED')))
  return 'CREATED'
}
return 'EXISTS'
```

**建连接（GitLabConnectionConfig）：**

```groovy
import jenkins.model.Jenkins
import com.dabsquared.gitlabjenkins.connection.GitLabConnectionConfig
import com.dabsquared.gitlabjenkins.connection.GitLabConnection
def d = Jenkins.get().getDescriptorByType(GitLabConnectionConfig)
d.setConnections([new GitLabConnection('gitlab', 'http://gitlab.devops.local:32037', 'gitlab-api-token', false, 10, 10)])
d.save()
return d.getConnections().collect { it.name + ' -> ' + it.url + ' (token ' + it.apiTokenId + ')' }.join('; ')
// → gitlab -> http://gitlab.devops.local:32037 (token gitlab-api-token)
```

> 构造参数含义：`(name, url, apiTokenId, ignoreCertificateErrors, connectionTimeout, readTimeout)`；`apiTokenId` 是 **credentialsId**（不是 token 本身）。

**连接活性验证（也是排障手法）**——scriptText 里走插件生产路径取 client，拿到 currentUser 即全链路通：

```groovy
def run = Jenkins.get().getItemByFullName('demo-app').getBuildByNumber(8)
def client = com.dabsquared.gitlabjenkins.connection.GitLabConnectionProperty.getClient(run)
return client.getCurrentUser().username   // → root
```

> 排障教训：`conn.getClient()` 没有零参签名（MissingMethodError）；`getClient(item, '名字')` 的第二个参数是 credentialsId 不是连接名，都会报错。生产路径只有 `GitLabConnectionProperty.getClient(run)`。

**连接建好后顺手修 root URL**（决定 commit status 的 target_url 等一切外链）：scriptText `JenkinsLocationConfiguration.get().setUrl('http://jenkins.devops.local:32037/'); JenkinsLocationConfiguration.get().save()`。缺端口 = GitLab 上的状态链接点不回去。

### 3. 事件通道：webhook（git push → 自动构建）

六步定位：**GitLab 放行内网回调 →（②③已做完）→ Jenkinsfile 声明 trigger → 跑一次注册 → GitLab 建 webhook**。

**④ Jenkinsfile 三件套**（语法见 02 章二，完整文件见 04 章二）：

```groovy
options    { gitLabConnection('gitlab') }
triggers   { gitlab(triggerOnPush: true, triggerOnMergeRequest: false, branchFilterType: 'All', secretToken: 'gltok-demo-2026') }
// 构建步骤外包：gitlabCommitStatus(name: 'build') { ... }
```

| 参数 | 本环境值 | 释义 |
|---|---|---|
| `triggerOnPush` | true | push 事件触发（core 事件，含 tag） |
| `triggerOnMergeRequest` | false | MR 事件触发，未启用 |
| `branchFilterType` | `'All'` | 全分支；`NameBased`/`RegexBased` 可过滤 |
| `secretToken` | `gltok-demo-2026` | webhook 投递令牌，两端一致才放行 |

**⑤ 跑一次 job 注册 trigger**：声明式 `triggers{}` 改完必须**手动跑一次**才写进 job config.xml（出现 `<com.dabsquared.gitlabjenkins.GitLabPushTrigger>` + 加密 `<secretToken>{AQAA…}`），光提交 Jenkinsfile 不生效。

**⑥ GitLab 建 webhook**（URL 必须是 `/project/<job 名>`；token 与 trigger 的 secretToken 一致）：

```bash
curl -s -X POST -H "PRIVATE-TOKEN: glpat-xxx" -H "Host: gitlab.devops.local" \
  -H "Content-Type: application/json" \
  http://123.58.219.112:32037/api/v4/projects/2/hooks \
  -d '{"url":"http://jenkins.devops.local:32037/project/demo-app","token":"gltok-demo-2026","push_events":true,"enable_ssl_verification":false}'
# 改 token：PUT /projects/2/hooks/1；投递记录：GET /projects/2/hooks/1/events（状态字段叫 response_status，不是 status）
```

> webhook URL 指向 `jenkins.devops.local:32037` 而非 svc 名：GitLab 要能解析（CoreDNS hosts 块，见 04 章一），且该域名与 Jenkins 的 Host 头分流一致。

**⑦ 端到端验证**：push 一个真实提交（本例 v3 改 index.html）→ Jenkins 自动排 build #8，构建原因 `Started by GitLab push by Administrator`；commit 状态回报：

```bash
# ⚠️ 坑：statuses 用短 SHA / 分支名查询会【静默返回 []】（POST 却收短 SHA）——必须完整 40 位 SHA
SHA=$(curl -s -H "PRIVATE-TOKEN: glpat-xxx" -H "Host: gitlab.devops.local" \
  http://123.58.219.112:32037/api/v4/projects/2/repository/commits/main \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
curl -s -H "PRIVATE-TOKEN: glpat-xxx" -H "Host: gitlab.devops.local" \
  http://123.58.219.112:32037/api/v4/projects/2/repository/commits/$SHA/statuses
# → [{"name":"build","status":"success","ref":"main","target_url":"http://jenkins.devops.local:32037/job/demo-app/8/…",…}]
```

## 五、API 实战速查

私有部署访问三要素：带 `Host` 头（ingress 分流）+ 走 EIP `123.58.219.112:32037` + `PRIVATE-TOKEN`。项目 id 拿法：`GET /api/v4/projects?search=demo-app`（/demo-app 的实况 id = 2）。

| 端点 | 用途 | 怪癖 |
|---|---|---|
| `GET /user` | 验 PAT | Basic Auth 已死，只用 PAT |
| `GET /projects?search=` / `GET /projects/:id` | 拿项目 id | — |
| `POST /projects` | 建项目 | `visibility=private` |
| `POST /projects/:id/repository/files` | API 提交文件 | body 里 `content` 要 JSON 转义，commit_message 必填 |
| `GET /projects/:id/repository/commits/main` | 拿最新提交 | `id` 字段才是全 SHA |
| `POST /projects/:id/statuses/:sha` | 报 commit 状态 | 收短 SHA |
| `GET /projects/:id/repository/commits/:sha/statuses` | 查 commit 状态 | **短 SHA/分支名静默返回 []，必须 40 位全 SHA** |
| `POST/PUT /projects/:id/hooks`、`GET /hooks/:id/events` | webhook CRUD 与投递记录 | events 状态字段叫 `response_status` |

## 六、GitLab 侧排障速查

| 症状 | 原因 → 修法 |
|---|---|
| rails 改设置报 `OpenSSL::CipherError` | /etc/gitlab 是 emptyDir，pod 重建后 secrets 重新生成，DB 旧加密列解不开 → `update_columns` 直写绕过 |
| 改完设置"读回来还是 false" | Redis 有 marshal 的 `application_setting:current` 旧副本 → `Rails.cache.delete` 后再验 |
| webhook 投递 403 `anonymous is missing the Job/Build permission` | gitlab-plugin 1.2154 `/project` 端点强制鉴权：trigger 配 `secretToken` + webhook 带同值 token；**与 Jenkins 全局授权无关别去调**；改 trigger 后必须手动跑一次 job 重新注册 |
| webhook 投递失败"禁止内网地址" | allow_local webhook 未放行（二.3） |
| webhook 建了但 Jenkins 收不到 | CoreDNS hosts 没配（GitLab 解析不了 jenkins.devops.local）→ 04 章一；投递记录看 `GET /hooks/:id/events` 的 `response_status` |
| PAT 明明正确却 401 | 用了 Basic Auth（16.0+ 已移除），换 `PRIVATE-TOKEN` 头 |
| git push 大对象 413 | ingress 默认 body 1m → 注解 `proxy-body-size: "0"`（装机已配，见 01 章二） |
| commit 状态一直 `[]` | 查询用了短 SHA/分支名（五的怪癖），换全 SHA |
