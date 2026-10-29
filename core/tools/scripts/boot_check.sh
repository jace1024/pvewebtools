
echo "启动模式 : $([ -d /sys/firmware/efi ] && echo EFI || echo Legacy BIOS)"
if [ -d /sys/firmware/efi ]; then
 REG=/etc/kernel/proxmox-boot-uuids
 if [ -s "$REG" ]; then
   echo "ESP 同步登记: ✓ 正常（已登记 $(wc -l < "$REG") 个分区）"
 else
   echo "ESP 同步登记: ✗ 缺失（$REG）——装新内核将不会自动同步到 EFI 分区，请执行'EFI 引导维护 → 修复 ESP 注册'"
 fi
fi
echo "当前内核 : $(uname -r)  架构: $(uname -m)"
echo; echo "---- 已安装内核 ----"
dpkg -l | grep '^ii' | grep -E '(proxmox-kernel|pve-kernel)-[0-9]' | awk '{print $2, $3}' | sort -V
echo; echo "---- 启动参数（GRUB_CMDLINE_LINUX_DEFAULT）----"
LINE=$(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub || true)
echo "${LINE:-未设置 GRUB_CMDLINE_LINUX_DEFAULT}"
echo
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
 echo "$LINE" | grep -q 'iommu=pt\|iommu.passthrough' && echo "✓ IOMMU 直通: 已开启" || echo "✗ IOMMU 直通: 未开启"
 echo "$LINE" | grep -q 'arm-smmu' && echo "✓ ARM-SMMU: 已开启" || echo "✗ ARM-SMMU: 未开启"
else
 echo "$LINE" | grep -q 'intel_iommu=on\|amd_iommu=on' && echo "✓ IOMMU 直通: 已开启" || echo "✗ IOMMU 直通: 未开启"
 echo "$LINE" | grep -q 'iommu=pt' && echo "✓ iommu=pt: 已开启" || echo "✗ iommu=pt: 未开启"
 echo "$LINE" | grep -q 'i915.enable_guc=3' && echo "✓ 核显 SR-IOV: 已开启" || echo "✗ 核显 SR-IOV: 未开启"
 echo "$LINE" | grep -q 'i915.enable_gvt=1' && echo "✓ 核显 GVT-g: 已开启" || echo "✗ 核显 GVT-g: 未开启"
fi
echo; echo "---- 现有 GRUB 备份 ----"
DIR=/etc/pvewebtools/backup/grub
if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
 ls -lh "$DIR" | tail -n +2
else
 echo "（无备份）"
fi
