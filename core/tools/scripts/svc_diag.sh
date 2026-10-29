
show_status() {
 echo "---- PVE 核心服务状态 ----"
 for s in pve-cluster pvedaemon pvestatd pveproxy pve-firewall; do
   ST=$(systemctl is-active $s 2>/dev/null || true)
   if [ "$ST" = "active" ]; then echo "  ✓ $s: 运行中"; else echo "  ✗ $s: ${ST:-unknown}"; fi
 done
 echo; echo "---- WebUI 端口 8006 ----"
 if ss -ltn 2>/dev/null | grep -q ':8006 '; then
   echo "  ✓ 8006 正在监听"
 else
   echo "  ✗ 8006 未监听（WebUI 无法访问）"
 fi
 FAILED=$(systemctl list-units --state=failed --no-legend 2>/dev/null || true)
 if [ -n "$FAILED" ]; then
   echo; echo "---- 失败单元 ----"; printf '%s\n' "$FAILED" | sed 's/^/  /'
 fi
}
if [ "$PT_ACTION" = "status" ]; then
 show_status
 exit 0
fi
if [ "$PT_ACTION" = "restart" ]; then
 echo "[1/2] 按依赖顺序重启核心服务..."
 for s in pve-cluster pvedaemon pvestatd pve-firewall pveproxy; do
   echo "  重启 $s ..."; run "重启 $s 失败" systemctl restart $s
 done
 sleep 2
 echo; echo "[2/2] 重启后状态："
 show_status
 exit 0
fi
echo "[错误] 未知操作: $PT_ACTION"; exit 1
