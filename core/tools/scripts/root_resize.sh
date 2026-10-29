
# ==================== PVE 根分区扩容 ====================

if [ "$PT_ACTION" = "del_swap" ]; then
 # ---- 删除 Swap 并扩容根分区 ----
 echo "===== [1/4] 检查 Swap 环境 ====="
 [ -e /dev/mapper/pve-swap ] || { echo "[错误] 不存在 LVM swap (/dev/mapper/pve-swap)"; exit 1; }
 echo "✓ 检测到 swap 逻辑卷: /dev/pve/swap"

 echo; echo "===== [2/4] 关闭并删除 Swap ====="
 run "swapoff 失败（可能仍被占用）" swapoff /dev/mapper/pve-swap
 echo "✓ swap 已关闭"
 backup_file /etc/fstab
 sed -i 's|^/dev/pve/swap|#/dev/pve/swap|' /etc/fstab
 echo "✓ fstab 已注释 swap 条目"
 run "lvremove 失败" lvremove -f /dev/pve/swap
 echo "✓ swap 逻辑卷已删除"

 echo; echo "===== [3/4] 扩容根分区 ====="
 run "lvextend 失败" lvextend -l +100%FREE /dev/mapper/pve-root
 echo "✓ LVM 扩容完成，正在扩展文件系统 ..."
 run "resize2fs 失败" resize2fs /dev/mapper/pve-root
 echo "✓ 文件系统已扩展"

 echo; echo "===== [4/4] 验证结果 ====="
 echo "--- 根分区容量 ---"
 df -h / | tail -1
 echo; echo "[完成] Swap 已删除，空间已全部并入根分区"
 exit 0
fi

if [ "$PT_ACTION" = "merge_local" ]; then
 # ---- 删除 local-lvm 并入 local ----
 echo "===== [1/5] 检查 LVM 环境 ====="
 lvdisplay /dev/pve/data >/dev/null 2>&1 || { echo "[错误] /dev/pve/data 不存在（可能已合并或非 LVM 安装）"; exit 1; }
 echo "✓ 检测到 local-lvm 逻辑卷: /dev/pve/data"

 # 数据占用检查：local-lvm 上仍被 VM/CT 磁盘引用时拒绝删除，避免误删虚机数据
 USED_VMS=$(grep -l "local-lvm:" /etc/pve/qemu-server/*.conf /etc/pve/lxc/*.conf 2>/dev/null | head -1 || true)
 [ -z "$USED_VMS" ] || { echo "[错误] local-lvm 上仍有虚机/容器磁盘（如 $(basename "$USED_VMS" .conf)），删除会丢失数据；请先把这些磁盘迁移到 local 或删除后再执行"; exit 1; }
 echo "✓ 未发现 VM/CT 磁盘引用 local-lvm"

 echo; echo "===== [2/5] 删除 local-lvm 逻辑卷并扩容根分区 ====="
 run "lvremove 失败" lvremove -f /dev/pve/data
 run "lvextend 失败" lvextend -l +100%FREE /dev/pve/root
 echo "✓ LVM 扩容完成，正在扩展文件系统 ..."
 run "resize2fs 失败" resize2fs /dev/pve/root
 echo "✓ 文件系统已扩展"
 df -h /

 echo; echo "===== [3/5] 更新 local 存储内容类型（启用 images/rootdir/vztmpl/iso/backup/snippets）====="
 run "更新 local 存储内容类型失败" pvesh set /storage/local --content images,rootdir,vztmpl,iso,backup,snippets
 echo "✓ local 存储已启用: images, rootdir, vztmpl, iso, backup, snippets"
 # 确保本工具的磁盘镜像暂存目录存在（统一在 cloud-images，隔离 PVE 系统目录）
 mkdir -p /var/lib/vz/cloud-images
 echo "✓ 本工具导入目录已就绪: /var/lib/vz/cloud-images/"
 # 同时保留 PVE WebUI 自带的「导入」功能所需目录
 mkdir -p /var/lib/vz/import

 echo; echo "===== [4/5] 从 PVE 中删除 local-lvm 存储条目 ====="
 if pvesh get /storage/local-lvm --output-format json >/dev/null 2>&1; then
   run "删除 local-lvm 存储条目失败" pvesh delete /storage/local-lvm
   echo "✓ local-lvm 存储条目已从 PVE 配置中删除"
 else
   echo "[提示] local-lvm 存储条目不存在或已被删除，跳过"
 fi

 echo; echo "===== [5/5] 验证结果 ====="
 echo "--- local 存储配置 ---"
 pvesh get /storage/local --output-format json 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f\"  路径  : {d.get('path','?')}\")
print(f\"  类型  : {d.get('type','?')}\")
print(f\"  内容  : {d.get('content','?')}\")
print(f\"  已启用: {'是' if not d.get('disable') else '否'}\")
" || echo "  （无法读取，请手动检查）"

 echo "--- 根分区容量 ---"
 df -h / | tail -1

 echo; echo "[完成] local-lvm 已合并到 local，空间已全部并入根分区"
 echo "[提示] PVE WebUI 会自动感知存储变更，刷新页面即可看到 local 已支持全部内容类型"
 exit 0
fi
echo "[错误] 未知操作: $PT_ACTION"; exit 1
