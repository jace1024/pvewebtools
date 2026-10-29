
echo "====== 硬件直通设备总览 ======"
# BDF/USB id → 设备名称（一次取全量，便于在原始配置行后补一句友好名称）
LSPCI_ALL=$(lspci -D 2>/dev/null || true)
LSUSB_ALL=$(lsusb 2>/dev/null || true)
FOUND=0
for conf in /etc/pve/qemu-server/*.conf; do
 [ -f "$conf" ] || continue
 VMID=$(basename "$conf" .conf)
 NAME=$(grep '^name:' "$conf" | sed 's/^name:[[:space:]]*//' || true)
 PT_LINES=$(grep -E '^(hostpci[0-9]+|usb[0-9]+):' "$conf" || true)
 RDM_LINES=$(grep -E '^(scsi|sata|virtio|ide)[0-9]+: */dev/' "$conf" || true)
 [ -n "$PT_LINES$RDM_LINES" ] || continue
 FOUND=$((FOUND+1))
 echo; echo "== VM $VMID${NAME:+（$NAME）} =="
 if [ -n "$PT_LINES" ]; then
  printf '%s\n' "$PT_LINES" | while IFS= read -r L; do
    DESC=""
    # PCIe 行：取 BDF（支持 hostpci0: 0000:02:00 这类省略 function 的写法）后查 lspci
    ADDR=$(printf '%s' "$L" | grep -oE '([0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(\.[0-9a-fA-F])?' | head -1 || true)
    if [ -n "$ADDR" ]; then
      DESC=$(printf '%s\n' "$LSPCI_ALL" | awk -v a="$ADDR" 'index($1,a)==1 { $1=""; sub(/^[ \t]+/,""); print substr($0,1,72); exit }')
    fi
    # USB 行（usb0: host=vid:pid）：用 lsusb 里同一 vid:pid 的名称
    if [ -z "$DESC" ]; then
      USBID=$(printf '%s' "$L" | grep -oE 'host=[0-9a-fA-F]{4}:[0-9a-fA-F]{4}' | cut -d= -f2 | tr 'A-F' 'a-f' || true)
      [ -n "$USBID" ] && DESC=$(printf '%s\n' "$LSUSB_ALL" | awk -v u="$USBID" 'tolower($6)==u { $1="";$2="";$3="";$4="";$5=""; sub(/^[ \t]+/,""); print substr($0,1,72); exit }')
    fi
    echo "   $L${DESC:+   ← $DESC}"
  done
 fi
 [ -n "$RDM_LINES" ] && echo "$RDM_LINES" | sed 's/^/   /; s/$/   <- RDM 磁盘/'
done
[ "$FOUND" -gt 0 ] || echo "（未发现任何直通设备）"
echo
echo "[结果] 共 $FOUND 个虚拟机配置了直通设备（容器 LXC 不支持 PCIe/USB 直通）"
