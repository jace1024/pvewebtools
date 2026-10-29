
if [ "$PT_ACTION" = "ipv6" ]; then
 echo "---- 全局 IPv6 地址 ----"
 ip -6 addr show scope global | grep inet6 || echo "(无全局 IPv6 地址)"
 echo; echo "---- IPv6 默认路由 ----"
 ip -6 route show default || echo "(无 IPv6 默认路由)"
 echo; echo "---- 外网连通性 (Cloudflare) ----"
 ping -6 -c 3 -W 2 2606:4700:4700::1111 2>/dev/null && echo "✓ IPv6 可用" || echo "✗ IPv6 不可用"
 exit 0
fi
[ -n "$PT_TARGET" ] || { echo "[错误] 请填写目标地址（IP 或域名）"; exit 1; }
echo "---- ping ----"; ping -c 4 -W 2 "$PT_TARGET" || true
echo; echo "---- DNS 解析 ----"; getent hosts "$PT_TARGET" || echo "(非域名或解析失败)"
