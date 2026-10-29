
IFACE=/etc/network/interfaces
case "$PT_ACTION" in
add_bridge)
 [ -n "$PT_PORT" ] || { echo "[错误] 请选择物理网口"; exit 1; }
 # 管理网口保护：承载默认路由的物理口加入网桥会立即改变其 IP 归属，网桥未配好地址即断网，先拦截
 GWDEV=$(ip route show default | awk '{print $5; exit}')
 [ "$GWDEV" != "$PT_PORT" ] || { echo "[错误] $PT_PORT 承载宿主默认路由（管理网），加入网桥会立即断网，已拒绝；如确需把管理网迁到网桥，请在物理控制台/WebUI 手工操作"; exit 1; }
 # 自动取下一个空闲 vmbrN，避免用户手填撞号
 BR=""
 for i in $(seq 0 99); do
   grep -q "^iface vmbr${i} " "$IFACE" || { BR="vmbr${i}"; break; }
 done
 [ -n "$BR" ] || { echo "[错误] 没有可用 vmbr 编号（vmbr0-99 已满）"; exit 1; }
 # 物理口已被配置中其他接口占用时拦截（如已是别的网桥的 bridge-ports）
 if grep -qw "$PT_PORT" "$IFACE"; then echo "[错误] 网口 $PT_PORT 已被配置中其他接口占用，请先确认"; exit 1; fi
 backup_file "$IFACE"
 cat >> "$IFACE" <<EOF

auto $BR
iface $BR inet static
   bridge-ports $PT_PORT
   bridge-stp off
   bridge-fd 0
EOF
 [ -n "$PT_CIDR" ] && echo "    address $PT_CIDR" >> "$IFACE"
 run "写入校验失败" grep -q "^iface $BR inet" "$IFACE"
 echo "[完成] 已创建 $BR（网口: $PT_PORT${PT_CIDR:+，IP: $PT_CIDR}）并写入配置"
 echo "[提示] 涉及管理网口时建议在物理控制台操作"
 ;;
del_bridge)
 [ -n "$PT_BRIDGE" ] || { echo "[错误] 请选择要删除的网桥"; exit 1; }
 grep -q "^iface $PT_BRIDGE " "$IFACE" || { echo "[错误] 配置中不存在网桥 $PT_BRIDGE"; exit 1; }
 # 双重保护：VM 在用拒删；承载宿主默认路由（管理网）的网桥拒删
 USED=$(grep -l "bridge=$PT_BRIDGE" /etc/pve/qemu-server/*.conf 2>/dev/null | head -1 || true)
 [ -z "$USED" ] || { echo "[错误] $PT_BRIDGE 正被 VM $(basename "$USED" .conf) 使用，请先改 VM 网络配置"; exit 1; }
 GWDEV=$(ip route show default | awk '{print $5; exit}')
 [ "$GWDEV" != "$PT_BRIDGE" ] || { echo "[错误] $PT_BRIDGE 承载宿主默认路由（管理网），删除会立即断网，已拒绝"; exit 1; }
 backup_file "$IFACE"
 TMP=$(pw_job_mktemp_file)
 # 段落模式（空行分段）删除该网桥的 iface 段与 auto 行
 awk -v RS='' -v ORS='\n\n' -v B="$PT_BRIDGE" '$0 !~ ("iface " B " ") && $0 !~ ("^auto " B "$")' "$IFACE" > "$TMP"
 run "配置重写失败" cp "$TMP" "$IFACE"
 rm -f "$TMP"
 if grep -q "^iface $PT_BRIDGE " "$IFACE"; then echo "[错误] 删除未生效，配置仍包含 $PT_BRIDGE"; exit 1; fi
 echo "[完成] 已从配置删除网桥 $PT_BRIDGE"
 ;;
add_vlan)
 [ -n "$PT_BRIDGE" ] || { echo "[错误] 请选择 VLAN 所在网桥"; exit 1; }
 [ -n "$PT_TAG" ] || { echo "[错误] 请填写 VLAN 标签"; exit 1; }
 [ "$PT_TAG" -le 4094 ] || { echo "[错误] VLAN 标签需在 1-4094"; exit 1; }
 [ -n "$PT_VCIDR" ] || { echo "[错误] 请填写 VLAN 接口 IP（CIDR）"; exit 1; }
 grep -q "^iface $PT_BRIDGE " "$IFACE" || { echo "[错误] 配置中不存在网桥 $PT_BRIDGE"; exit 1; }
 grep -q "^iface $PT_BRIDGE.$PT_TAG " "$IFACE" && { echo "[错误] VLAN $PT_BRIDGE.$PT_TAG 已存在"; exit 1; }
 backup_file "$IFACE"
 cat >> "$IFACE" <<EOF

auto $PT_BRIDGE.$PT_TAG
iface $PT_BRIDGE.$PT_TAG inet static
   vlan-raw-device $PT_BRIDGE
   address $PT_VCIDR
EOF
 run "写入校验失败" grep -q "^iface $PT_BRIDGE.$PT_TAG inet" "$IFACE"
 echo "[完成] 已创建 VLAN 接口 $PT_BRIDGE.$PT_TAG（IP: $PT_VCIDR）"
 echo "[提示] 对端交换机口需放行 VLAN $PT_TAG"
 ;;
del_vlan)
 [ -n "$PT_VLAN" ] || { echo "[错误] 请选择要删除的 VLAN 接口"; exit 1; }
 grep -q "^iface $PT_VLAN " "$IFACE" || { echo "[错误] 配置中不存在 VLAN 接口 $PT_VLAN"; exit 1; }
 backup_file "$IFACE"
 TMP=$(pw_job_mktemp_file)
 # VLAN 名含点号，先转义再作正则匹配，避免误删同前缀接口
 ESC=$(printf '%s' "$PT_VLAN" | sed 's/\./\\./g')
 awk -v RS='' -v ORS='\n\n' -v V="$ESC" '$0 !~ ("iface " V " ") && $0 !~ ("^auto " V "$")' "$IFACE" > "$TMP"
 run "配置重写失败" cp "$TMP" "$IFACE"
 rm -f "$TMP"
 if grep -q "^iface $PT_VLAN " "$IFACE"; then echo "[错误] 删除未生效，配置仍包含 $PT_VLAN"; exit 1; fi
 echo "[完成] 已从配置删除 VLAN 接口 $PT_VLAN"
 ;;
*)
 echo "[错误] 未知操作: $PT_ACTION"; exit 1
 ;;
esac

# 是否立即生效：默认勾选则应用配置；取消勾选则只写文件，留给「网络配置管理」手动应用
if [ "$PT_APPLY_NOW" = "1" ]; then
 echo; echo "===== 应用网络配置 ====="
 if command -v ifreload >/dev/null 2>&1; then
   run "ifreload 应用失败（若已断网请到物理控制台，或用「网络配置管理」从备份恢复）" ifreload -a
   echo "[完成] 网络配置已生效（ifreload -a）"
 else
   run "重启网络服务失败（若已断网请到物理控制台恢复）" systemctl restart networking
   echo "[完成] 已重启 networking 服务，配置已生效"
 fi
 echo; ip -br addr
else
 echo "[提示] 配置已写入但尚未生效，请到「网络配置管理」执行「应用配置」使其生效"
fi
