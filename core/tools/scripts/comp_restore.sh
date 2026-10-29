
if [ "$PT_ACTION" = "webui" ]; then
 run "重装失败，请检查源配置" env DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y pve-manager proxmox-widget-toolkit
 systemctl restart pveproxy
 echo "[完成] WebUI 文件已恢复官方版本，请清除浏览器缓存"
 exit 0
fi
if [ "$PT_ACTION" = "qemu" ]; then
 apt-mark unhold pve-qemu-kvm 2>/dev/null || true
 apt-get update
 run "重装失败，请检查源配置" env DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y pve-qemu-kvm
 echo "[完成] 官方 QEMU 已恢复，建议重启宿主机"
 exit 0
fi
echo "[错误] 未知操作: $PT_ACTION"; exit 1
