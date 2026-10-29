
JS=/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
[ -f "$JS" ] || { echo "[错误] 未找到 $JS"; exit 1; }
backup_file "$JS"

# 落地持久化补丁脚本（幂等），供本工具立即执行，并挂到 apt hook 供升级后自动重打
HOOK=/usr/local/sbin/pvewebtools-reapply-popup
cat > "$HOOK" <<'PATCH_SCRIPT'
#!/bin/bash
# 由 PVEWEBTOOLS 生成：幂等重打订阅弹窗补丁（供 apt hook 与手动调用）
# 移除方法：删除本文件与 /etc/apt/apt.conf.d/99pvewebtools-popup
JS=/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
[ -f "$JS" ] || exit 0

# 兼容迁移：旧版以 PVE-WEBTOOLS 标记注入的补丁，统一改写为新版 PVEWEBTOOLS 标记。
# 否则旧主机重跑时 grep 旧标记失败，会重复注入 return; 或追加重复的全局拦截块。
# （proxmoxlib.js 官方内容不含此字符串，仅本工具补丁使用，整文件替换安全）
sed -i 's/PVE-WEBTOOLS/PVEWEBTOOLS/g' "$JS" 2>/dev/null || true

grep -q "PVEWEBTOOLS" "$JS" 2>/dev/null && exit 0

# 策略 A：PVE 8.x 经典写法（subscription status !== 'active'）
if grep -q "res.data.status.toLowerCase() !== 'active'" "$JS"; then
 sed -i "s/res.data.status.toLowerCase() !== 'active'/res.data.status.toLowerCase() == 'active' \/* PVEWEBTOOLS *\//g" "$JS"
# 策略 B：Ext.Msg.show 弹窗（老版本）
elif grep -q "Ext.Msg.show({" "$JS"; then
 perl -i -0777 -pe "s/(Ext\.Msg\.show\(\{\s+title: gettext\('No valid sub)/void\(\{ \/\/\1/g" "$JS"
# 策略 C：checkSubscription 函数（PVE 9.x 新写法）
elif grep -q "checkSubscription\|status.*subscription\|subscription.*status" "$JS"; then
 perl -i -0777 -pe 's/(function\s+checkSubscription\s*\([^)]*\)\s*\{)/\1\n\treturn; \/\* PVEWEBTOOLS \*\//' "$JS"
fi

# 策略 D：以上均未命中时，在文件末尾追加全局拦截
if ! grep -q "PVEWEBTOOLS" "$JS"; then
 printf '%s\n' \
   '/* PVEWEBTOOLS: 全局拦截订阅弹窗 */' \
   "if (typeof Proxmox !== 'undefined' && Proxmox.Utils) {" \
   "  Proxmox.Utils.checked_command = function(orig) { return function() { return true; }; }(Proxmox.Utils.checked_command);" \
   '}' >> "$JS"
fi

# 补丁真正写入后才重启 pveproxy（未改动则不重启，保持幂等）
grep -q "PVEWEBTOOLS" "$JS" && systemctl restart pveproxy.service 2>/dev/null
exit 0
PATCH_SCRIPT
chmod +x "$HOOK"

# 立即执行一次打补丁
"$HOOK"

# 写后验证
if grep -q "PVEWEBTOOLS" "$JS"; then
 echo "[成功] 订阅弹窗补丁已写入"
else
 echo "[错误] 修改未生效，补丁标记未写入 $JS"
 echo "PVE 版本: $(pveversion 2>/dev/null || echo 未知)"
 echo "proxmoxlib.js MD5: $(md5sum "$JS" | awk '{print $1}')"
 exit 1
fi

# 安装 apt hook：dpkg 每次操作完成后自动重打补丁（升级 proxmox-widget-toolkit 不再回弹）
APT_HOOK=/etc/apt/apt.conf.d/99pvewebtools-popup
if [ ! -f "$APT_HOOK" ]; then
 echo 'DPkg::Post-Invoke { "/usr/local/sbin/pvewebtools-reapply-popup"; };' > "$APT_HOOK"
fi

sleep 2
run "pveproxy 重启后未处于运行状态" systemctl is-active --quiet pveproxy
echo "[完成] 订阅弹窗已删除，请清除浏览器缓存后刷新 WebUI"
echo "[提示] 已安装持久化 hook，今后升级 proxmox-widget-toolkit 会自动重打补丁"
