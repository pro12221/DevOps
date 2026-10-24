# kubebuilder 生成的 Makefile：要不要先看懂？

> 图使用 Mermaid 绘制并导出 PNG，源文件见 `diagrams/*.mmd`。

## 一句话结论

**先忽略 90%，但要拿下 5 个按钮和 1 个心智模型。** 逐行读这个 Makefile 是 kubebuilder 学习里性价比最低的动作——它 2/3 的篇幅在解决"给不同机器装同一版本的工具"和"CI 怎么跑"，跟你要学的 Operator 逻辑无关。等你能跑通 guestbook 主线、并且被"生成物"绊过一次之后，再回读，10 分钟就通了。

## 为什么 kubebuilder 一定要带 Makefile

kubebuilder 给的不只是骨架代码，而是一套**代码生成契约**。CRD 的真源不是 YAML，是你写的 Go：

```go
// api/v1/guestbook_types.go —— 这是真源
type GuestbookSpec struct {
    // +optional
    Foo *string `json:"foo,omitempty"`   // json tag 和 marker 决定 CRD schema
}
```

`config/crd/bases/*.yaml`、`zz_generated.deepcopy.go`、`config/rbac/role.yaml` 全是**派生物**。改了 struct 不重新生成，集群里的 schema 就是旧的，`kubectl apply` 会被 API Server 拒绝或静默丢字段——这是新手第一个大坑。

Makefile 的职责就是把这套"改源 → 生成 → 编译 → 测试 → 打包 → 部署"的链条固定下来，让你不用记 controller-gen 那一长串参数。

## 数据流：Makefile 到底在串什么

这张图回答：我改了 Go 文件之后，哪些命令会把改动"传导"到集群。

![Makefile 数据流](diagrams/makefile-dataflow.png)

关键点：**`make build` 不是纯编译**。它的依赖是 `manifests generate fmt vet`，也就是说每次 build 都会先重新生成 YAML 和 DeepCopy。所以跑完 `make build` 发现 `git status` 变脏是正常的，那些改动应该一起提交。

## 这个 Makefile 的四段结构

`make help` 就是目录（它靠 awk 解析 `##` 注释生成）：

| 段 | 目标 | 你在教程里的使用频率 |
|---|---|---|
| Development | `manifests` `generate` `fmt` `vet` `test` `lint` | 高 |
| Build | `build` `run` `docker-build` `docker-push` | 高 |
| Deployment | `install` `deploy` `undeploy` | 中 |
| Dependencies | `controller-gen` `kustomize` `envtest` `golangci-lint` | 低（自动触发） |

## 只需要 5 个按钮

| 命令 | 实际做什么 | 什么时候敲 | 副作用 |
|---|---|---|---|
| `make manifests` | controller-gen 读 `+kubebuilder:` 标记 → 重写 `config/crd/bases/*.yaml`、`config/rbac/role.yaml` | 改完 `*_types.go` 的字段/marker 后 | 改生成物 |
| `make generate` | 生成 `zz_generated.deepcopy.go` | 同上 | 改生成物 |
| `make test` | 先下载 envtest 的 kube-apiserver/etcd，再用它们跑单测 | 改完 Reconcile | 首次下载几百 MB |
| `make run` | `go run ./cmd/main.go`，用你本机 kubeconfig 连集群 | 本地调 Reconcile（最快） | 需要 kubeconfig |
| `make install` | 把 CRD 装进集群 | 第一次部署前 | 改集群 |

`make manifests generate` 这俩几乎总是一起敲，可以记成一条。

## make 语法最小集（够读这个文件）

| 写法 | 含义 | 文件里的例子 |
|---|---|---|
| `VAR ?= x` | 没定义过才赋值；**命令行传的值优先** | `IMG ?= controller:latest` |
| `VAR = x` | 递归展开：用到时才求值 | `GOBIN=$(shell go env GOPATH)/bin` |
| `VAR := x` | 立即展开：读到这行就算好 | `.SHELLFLAGS = -ec` |
| `目标: 依赖` | 跑目标前先跑依赖（递归） | `build: manifests generate fmt vet` |
| recipe | 目标下的命令行，**必须 TAB 开头** | `go build -o bin/manager cmd/main.go` |
| `.PHONY: x` | 声明 x 不是文件，永远执行 | 见下面坑 1 |
| `$@` `$<` `$^` | 目标名 / 第一个依赖 / 全部依赖 | 本文件没用，别的项目常见 |
| `$(shell ...)` | 调 shell 取输出 | `$(shell pwd)/bin` |
| `$(call 宏,参数)` | 调用 `define` 定义的宏，`$(1)` 取参 | `$(call go-install-tool,...)` |
| recipe 前缀 `@` | 不回显命令本身 | `@awk '...'` |
| recipe 前缀 `-` | 该行失败也继续 | `- $(CONTAINER_TOOL) buildx create` |

一个目标长这样，三行就是全部语法：

```make
.PHONY: build
build: manifests generate fmt vet   # 冒号后是依赖，会先执行
	go build -o bin/manager cmd/main.go   # 缩进必须是 TAB
```

## 学习顺序

![学习顺序](diagrams/makefile-learning-path.png)

## 6 个具体坑（都来自这个文件）

1. **`.PHONY: test` 不是装饰**。项目里有 `test/` 这个目录（e2e 用）。如果不声明 PHONY，make 看到同名目录存在会说 `make: 'test' is up to date.` 然后什么都不干。
2. **recipe 每行是独立的 shell**。`cd config/manager && kustomize ...` 必须写在一行里，不能靠上一行的 `cd`。这就是 `deploy` 里为什么用 `&&` 拼接。
3. **`.SHELLFLAGS = -ec`**：`-e` 让任何一步失败立刻停，`-c` 是传给 bash 的。所以多步命令里 `-` 前缀（忽略错误）才显得重要。
4. **`$(shell ...)` 在解析阶段就会跑**。文件顶部的 `ifeq (,$(shell go env GOBIN))` 是读入时立即求值的（实测：把 `go` 换成会写日志的假程序后跑 `make help`，日志里出现了 `env GOBIN`）；`$(LOCALBIN):` 这种目标行也在读入时展开，所以 `pwd` 同样会执行。这也解释了 `ENVTEST_K8S_VERSION` 为什么要用 `go list -m` 反查——一旦 `go list` 失败（没网、模块缓存空），变量变成空字符串，不会报错，而是静默装错版本。
5. **`make deploy` 会改你的仓库**：`cd config/manager && kustomize edit set image controller=${IMG}` 直接改写 `config/manager/kustomization.yaml`。`build-installer` 同理。
6. **`go-install-tool` 的版本把戏**：工具装成 `bin/controller-gen-v0.22.0`，再用符号链接 `bin/controller-gen` 指过去。升级版本只改 `CONTROLLER_TOOLS_VERSION`，旧文件留着不会冲突。`bin/*` 在 `.gitignore` 里，所以队友第一次 `make` 会各自下载。

## 现在该做什么

你的项目在 `DevOps/kubernetes/Operator模式/kubebuilder/guestbook`，`Reconcile` 还是空的 TODO。按这个顺序：

```bash
# 1. 先编辑 api/v1/guestbook_types.go，加你真正想要的字段
# 2. 生成（每改一次类型都要跑）
make manifests generate
# 3. 看一眼生成物，建立"生成物不能手改"的直觉
git diff config/crd/bases/ api/v1/zz_generated.deepcopy.go
# 4. 编辑 internal/controller/guestbook_controller.go 的 Reconcile
make test
make install
make run        # 另开一个终端 kubectl apply -k config/samples/
```

碰到 `make xxx` 报错再回来查这张表，不用提前背。

## 延伸阅读

- GNU Make 手册，重点看 *Recipe Syntax*（TAB、`@`、`-`）和 *Flavors*（`=` vs `:=`）：https://www.gnu.org/software/make/manual/html_node/Recipe-Syntax.html
- kubebuilder 书里讲生成物与 marker 的部分：https://book.kubebuilder.io/reference/markers.html
- 本项目的 `AGENTS.md` 已经列了哪些文件"DO NOT EDIT"，就是上面那张数据流图的文字版。
