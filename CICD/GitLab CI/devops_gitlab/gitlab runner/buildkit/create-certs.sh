#!/usr/bin/env bash
# 生成 buildkitd 的 mTLS 自签证书，用于 deploy.yaml 里挂载的 buildkit-daemon-certs secret。
#
# 产出（默认落在 ./certs）：
#   ca.pem     CA 根证书     —— daemon 用它验证客户端
#   cert.pem   服务端/客户端证书 —— 必须带 SAN，覆盖客户端连接用的名字
#   key.pem    私钥
#   ca-key.pem CA 私钥（不进 secret，仅签发用，能重建整套信任链）
#
# 用法：
#   ./create-certs.sh              # 输出到 ./certs
#   ./create-certs.sh /tmp/bk      # 指定输出目录
#
# 然后建 secret（键名必须是 ca.pem/cert.pem/key.pem）：
#   kubectl -n <namespace> create secret generic buildkit-daemon-certs \
#     --from-file=ca.pem --from-file=cert.pem --from-file=key.pem
set -euo pipefail

cd "$(dirname "$0")"
OUT="${1:-certs}"

rm -rf "$OUT"
mkdir -p "$OUT"

# SAN 覆盖所有可能的连接名：ClusterIP Service 名、带 namespace 的 FQDN、本机。
# 少了客户端实际用的名字，TLS 校验会报 "x509: certificate is valid for ..., not buildkitd"。
cat > "$OUT/san.cnf" <<'EOF'
[req]
distinguished_name = dn
req_extensions     = ext
prompt             = no
[dn]
CN = buildkitd
[ext]
subjectAltName = DNS:buildkitd,DNS:buildkitd.default.svc,DNS:buildkitd.default.svc.cluster.local,DNS:buildkitd.devops.svc,DNS:buildkitd.devops.svc.cluster.local,DNS:localhost,IP:127.0.0.1
EOF

echo "==> CA"
openssl genrsa -out "$OUT/ca-key.pem" 2048
openssl req -new -x509 -days 3650 -key "$OUT/ca-key.pem" -out "$OUT/ca.pem" -subj "/CN=buildkitd-ca"

echo "==> 签发服务端/客户端证书（含 SAN）"
openssl genrsa -out "$OUT/key.pem" 2048
openssl req -new -key "$OUT/key.pem" -out "$OUT/cert.csr" -config "$OUT/san.cnf"
openssl x509 -req -in "$OUT/cert.csr" -CA "$OUT/ca.pem" -CAkey "$OUT/ca-key.pem" -CAcreateserial \
  -out "$OUT/cert.pem" -days 3650 -sha256 -extensions ext -extfile "$OUT/san.cnf"

echo "==> 校验"
openssl verify -CAfile "$OUT/ca.pem" "$OUT/cert.pem"
openssl x509 -in "$OUT/cert.pem" -noout -subject -ext subjectAltName

rm -f "$OUT/cert.csr" "$OUT/ca.srl" "$OUT/ca.pem.srl"
echo
echo "完成。证书在 $OUT/"
ls -1 "$OUT"/*.pem
