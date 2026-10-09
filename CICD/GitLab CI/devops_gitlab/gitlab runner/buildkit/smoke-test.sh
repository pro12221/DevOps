#!/usr/bin/env bash
# buildkitd 冒烟测试 —— 验证 devops/buildkitd 的 TCP + mTLS 通路是否正常。
#
# 前置：
#   - kubectl 已指向目标集群
#   - devops 命名空间里有 deploy.yaml 拉起的 buildkitd，且 buildkit-daemon-certs 已创建
#
# 用法：
#   ./smoke-test.sh
#   NS=devops ADDR=tcp://buildkitd.devops.svc:1234 ./smoke-test.sh
#
# 测试项：
#   1. Pod 就绪
#   2. 带 mTLS 走 TCP 列出 worker        —— 连通 + 双向认证
#   3. 不带客户端证书                    —— 应被拒（证明是双向认证）
#   4. 明文连接（不带任何 TLS 参数）      —— 应被拒（证明 TCP 端点是 TLS-only）
#   5. 走 Service DNS 名做一次真实构建    —— 端到端 + SAN 的 DNS 名可用
set -euo pipefail

NS="${NS:-devops}"
ADDR="${ADDR:-tcp://buildkitd.devops.svc:1234}"
CA=/certs/ca.pem; CERT=/certs/cert.pem; KEY=/certs/key.pem

fail=0
pass() { printf '  \033[32m✓ %s\033[0m\n' "$1"; }
bad()  { printf '  \033[31m✗ %s\033[0m\n' "$1"; fail=1; }

POD="$(kubectl -n "$NS" get po -l app=buildkitd -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -z "$POD" ]; then
  echo "找不到 $NS 下的 buildkitd Pod，先 apply deploy.yaml" >&2
  exit 2
fi
echo "buildkitd pod: $POD  (ns=$NS, addr=$ADDR)"

exec_pod() { kubectl -n "$NS" exec "$POD" -- "$@"; }
TLS=(--addr "$ADDR" --tlscacert "$CA" --tlscert "$CERT" --tlskey "$KEY")

echo "[1] Pod 就绪"
ready="$(kubectl -n "$NS" get po "$POD" -o jsonpath='{.status.containerStatuses[0].ready}')"
[ "$ready" = "true" ] && pass "Ready=$ready" || bad "Ready=$ready"

echo "[2] mTLS 连通（debug workers）"
if out="$(exec_pod buildctl "${TLS[@]}" debug workers 2>&1)"; then
  pass "worker: $(echo "$out" | awk 'NR==2{print $1}')"
else
  bad "连接失败: $out"
fi

echo "[3] 缺客户端证书应被拒"
if exec_pod buildctl --addr "$ADDR" --tlscacert "$CA" debug workers >/dev/null 2>&1; then
  bad "竟然连上了，mTLS 没生效"
else
  pass "已拒绝"
fi

echo "[4] 明文连接应被拒"
if exec_pod buildctl --addr "$ADDR" debug workers >/dev/null 2>&1; then
  bad "竟然连上了，TCP 端点没强制 TLS"
else
  pass "已拒绝"
fi

echo "[5] 走 Service DNS 真实构建（scratch + COPY）"
if exec_pod sh -c "
  set -e
  rm -rf /tmp/smoke-ctx /tmp/smoke-out; mkdir -p /tmp/smoke-ctx /tmp/smoke-out
  printf 'smoke-ok\n' > /tmp/smoke-ctx/hello.txt
  printf 'FROM scratch\nCOPY hello.txt /\n' > /tmp/smoke-ctx/Dockerfile
  buildctl --addr $ADDR --tlscacert $CA --tlscert $CERT --tlskey $KEY \
    build --frontend dockerfile.v0 \
    --local context=/tmp/smoke-ctx --local dockerfile=/tmp/smoke-ctx \
    --output type=local,dest=/tmp/smoke-out >/dev/null 2>&1
  [ \"\$(cat /tmp/smoke-out/hello.txt)\" = smoke-ok ]
"; then
  pass "构建成功，产物内容正确"
else
  bad "构建失败"
fi

echo
[ "$fail" -eq 0 ] && { printf '\033[32m全部通过\033[0m\n'; exit 0; } || { printf '\033[31m有失败项\033[0m\n'; exit 1; }
