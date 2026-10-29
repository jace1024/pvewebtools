
if [ "$PT_ACTION" = "config" ]; then
 echo "---- /etc/network/interfaces ----"
 cat /etc/network/interfaces
 exit 0
fi
echo "---- 接口状态 ----"; ip -br addr
echo; echo "---- 网桥 ----"; ip -br link show type bridge 2>/dev/null
echo; echo "---- 默认路由 ----"; ip route show default; ip -6 route show default 2>/dev/null
echo; echo "---- 监听端口 (前20) ----"; ss -lntup 2>/dev/null | head -21
