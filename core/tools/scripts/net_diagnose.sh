
# 精简网络诊断：一屏展示关键信息
# 对应 net_info(status) + net_ping(ping) + net_ping(ipv6) 的聚合精简版
# 注意：脚本在 set -e 环境下运行，所有可能返回非零的命令须加 || true 兜底

echo "═══════ 网络诊断 ═══════"

# 1) 接口摘要（仅列接口名 / 状态 / IP）
echo "── 接口 ──"
ip -br addr 2>/dev/null | awk '{
 iface=$1; state=$2;
 addr="";
 for(i=3;i<=NF;i++) addr=addr" "$i;
 gsub(/^ +/,"",addr);
 printf "  %-12s %-6s %s\n", iface, state, (addr!=""?addr:"-")
}'

# 2) 网桥（仅名称）
B=$(ip -br link show type bridge 2>/dev/null | awk '{printf "%s ",$1}' || true)
if [ -n "$B" ]; then
 echo "── 网桥 ──  $B"
else
 echo "── 网桥 ──  (无)"
fi

# 3) 默认路由（IPv4/IPv6 各一行）
echo "── 默认路由 ──"
V4_ROUTE=$(ip route show default 2>/dev/null | head -1 || true)
[ -n "$V4_ROUTE" ] && echo "  IPv4: $V4_ROUTE" || echo "  IPv4: (无)"
V6_ROUTE=$(ip -6 route show default 2>/dev/null | head -1 || true)
[ -n "$V6_ROUTE" ] && echo "  IPv6: $V6_ROUTE" || echo "  IPv6: (无)"

# 4) 连通性测试（ping 4 次，取丢包率+延迟）
echo "── 连通性 ──"
PING_HOST=223.5.5.5
PING_R=$(ping -c 4 -W 2 "$PING_HOST" 2>/dev/null || true)
LOSS=$(echo "$PING_R" | sed -n 's/.*\([0-9]\+\)% packet loss.*/\1/p' || true)
RTT=$(echo "$PING_R" | sed -n 's#.*/\([0-9.]\+\)/[0-9.]\+/\(dev\|mdev\).*#\1#p' || true)
if [ -n "$LOSS" ] && [ "$LOSS" != "100" ]; then
 echo "  $PING_HOST  ✓  丢包 ${LOSS}%  平均延迟 ${RTT:-N/A}ms"
else
 echo "  $PING_HOST  ✗ 不可达"
fi

# 5) DNS 解析
echo "── DNS ──"
DNS_OK=$(getent hosts www.baidu.com 2>/dev/null | head -1 || true)
if [ -n "$DNS_OK" ]; then
 echo "  解析    ✓ $(echo "$DNS_OK" | awk '{print $1}')"
else
 echo "  解析    ✗ 失败"
fi

# 6) IPv6 就绪度（一行汇总）
echo "── IPv6 ──"
V6_COUNT=$(ip -6 addr show scope global 2>/dev/null | grep -c 'inet6' || true)
V6_COUNT=${V6_COUNT:-0}
V6_RT=$(ip -6 route show default 2>/dev/null | head -1 || true)
if [ "$V6_COUNT" -gt 0 ] && [ -n "$V6_RT" ]; then
 V6_PING=$(ping -6 -c 2 -W 2 2606:4700:4700::1111 2>/dev/null && echo ok || true)
 if [ -n "$V6_PING" ]; then
   echo "  IPv6  ✓ 可用 (地址 ${V6_COUNT} 条, 路由已配置)"
 else
   echo "  IPv6  △ 有地址/路由但外网不通"
 fi
else
 RT_STATUS="无路由"
 [ -n "$V6_RT" ] && RT_STATUS="路由已配置"
 echo "  IPv6  ✗ 不可用 (地址 ${V6_COUNT} 条, ${RT_STATUS})"
fi

echo "═════════════════════════"
