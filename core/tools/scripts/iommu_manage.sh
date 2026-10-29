
if [ "$PT_ACTION" = "enable" ]; then
echo "====== 开启硬件直通 ======"
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
 PARAM="iommu=pt"
elif grep -qi intel /proc/cpuinfo; then PARAM="intel_iommu=on iommu=pt"
else PARAM="amd_iommu=on iommu=pt"; fi
echo "[1/5] 写入 IOMMU 内核启动参数（引导方式：$(boot_param_mode)）: $PARAM"
# 先按「键名」清掉旧值（如 intel_iommu=off / iommu=soft），再幂等追加，避免同名参数并存产生歧义
boot_param_del intel_iommu amd_iommu iommu iommu.passthrough || exit 1
boot_param_add "$PARAM" || exit 1
echo "[2/5] 写入 vfio 模块（开机预加载，优先于显卡驱动绑定设备）"
apply_block /etc/modules IOMMU_VFIO "vfio
vfio_iommu_type1
vfio_pci"
if [ "$PT_BLACKLIST_DGPU" = "yes" ]; then
 echo "[3/5] 屏蔽独显驱动（nouveau/nvidia/amdgpu/radeon）"
 apply_block /etc/modprobe.d/pvewebtools-blacklist.conf DGPU_BLACKLIST "blacklist nouveau
blacklist nvidia
blacklist nvidiafb
blacklist amdgpu
blacklist radeon"
else
 echo "[3/5] 跳过独显驱动屏蔽"
fi
if [ "$PT_BLACKLIST_IGPU" = "yes" ]; then
 echo "[4/5] 屏蔽核显/声卡驱动（Intel i915 / AMD APU amdgpu + 声卡）"
 # AMD APU 核显与 AMD 独显同为 amdgpu 驱动（老 APU 为 radeon）：若上面已勾选「屏蔽独显驱动」，
 # amdgpu/radeon 已在该黑名单块里，这里不再重复写入；未勾选时才追加，确保 AMD 核显也能被解绑。
 IGPU_BL="blacklist i915
blacklist snd_hda_intel
blacklist snd_hda_codec_hdmi"
 if [ "$PT_BLACKLIST_DGPU" != "yes" ]; then
   IGPU_BL="$IGPU_BL
blacklist amdgpu
blacklist radeon"
 fi
 apply_block /etc/modprobe.d/pvewebtools-blacklist.conf IGPU_BLACKLIST "$IGPU_BL"
 if [ "$PT_BLACKLIST_DGPU" != "yes" ]; then
   echo "[提示] 宿主机为 AMD 平台时核显与独显共用 amdgpu/radeon，本次已一并屏蔽（宿主机将不再使用 AMD 显卡）"
 fi
else
 echo "[4/5] 跳过核显驱动屏蔽"
fi
echo "[5/5] 更新 initramfs"
run "update-initramfs 执行失败" update-initramfs -u -k all
# 写后验证：启动参数与 vfio 模块块确实落盘（按引导方式回读，不只认 GRUB）
MISS=""
for a in $PARAM; do printf '%s' "$(boot_param_read)" | grep -qF "$a" || MISS="$MISS $a"; done
if [ -n "$MISS" ]; then
 echo "[错误] 内核启动参数写入未生效（缺:$MISS，引导方式：$(boot_param_mode)）"; exit 1
fi
if ! grep -q "# PVEWEBTOOLS BEGIN IOMMU_VFIO" /etc/modules; then
 echo "[错误] vfio 模块写入未生效"; exit 1
fi
echo "=============================================="
echo "[结果] 硬件直通全部前提已配置完成（重启后生效，可点弹窗内「重启节点生效配置」按钮）"
echo "  - IOMMU 启动参数：$PARAM（$(boot_param_mode)）"
echo "  - vfio 模块：已写入 /etc/modules"
[ "$PT_BLACKLIST_DGPU" = "yes" ] && echo "  - 独显驱动屏蔽：已启用（nouveau/nvidia/nvidiafb/amdgpu/radeon）"
[ "$PT_BLACKLIST_IGPU" = "yes" ] && echo "  - 核显/声卡驱动屏蔽：已启用（i915 + snd_hda；AMD 平台核显 amdgpu/radeon 一并覆盖）"
echo "=============================================="
exit 0
fi

if [ "$PT_ACTION" = "disable" ]; then
echo "====== 一键关闭并还原全部直通配置 ======"
echo "[1/4] 移除 IOMMU 内核启动参数（引导方式：$(boot_param_mode)）..."
# 同时覆盖 ARM 的 iommu.passthrough 写法
boot_param_del intel_iommu amd_iommu iommu iommu.passthrough || exit 1
echo "[2/4] 移除 vfio 模块块与驱动黑名单 ..."
remove_block /etc/modules IOMMU_VFIO
remove_block /etc/modprobe.d/pvewebtools-blacklist.conf IGPU_BLACKLIST
remove_block /etc/modprobe.d/pvewebtools-blacklist.conf DGPU_BLACKLIST
echo "[3/4] 更新 initramfs ..."
run "update-initramfs 执行失败" update-initramfs -u -k all
# 写后验证：直通参数与模块块确实已清除（按引导方式回读，不只认 GRUB）
if printf '%s' "$(boot_param_read)" | grep -qE '(intel|amd)_iommu'; then
 echo "[错误] 内核启动参数移除未生效（引导方式：$(boot_param_mode)）"; exit 1
fi
if grep -q "# PVEWEBTOOLS BEGIN IOMMU_VFIO" /etc/modules; then
 echo "[错误] vfio 模块块移除未生效"; exit 1
fi
echo "[4/4] 完成"
echo "=============================================="
echo "[结果] 硬件直通: 已全部关闭并还原（含驱动黑名单，重启后生效）"
echo "=============================================="
exit 0
fi

echo "[错误] 未知操作: $PT_ACTION"; exit 1
