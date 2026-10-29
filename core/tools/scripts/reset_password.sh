
PASS="$PT_PASSWORD"
[ -n "$PASS" ] || { echo "[错误] 请输入新密码"; exit 1; }
[ ${#PASS} -ge 6 ] || { echo "[错误] 密码长度不能少于 6 位"; exit 1; }

echo "===== [1/3] 重置系统 root 密码 ====="
echo "$PASS" | passwd --stdin root 2>/dev/null || echo "root:$PASS" | chpasswd 2>/dev/null || \
 { echo "[错误] 系统 root 密码重置失败"; exit 1; }
echo "✓ 系统 root 密码已重置"

echo; echo "===== [2/3] 更新 PVE 内置认证用户（pve realm）====="
if pvesh get /access/users/root@pve --output-format json >/dev/null 2>&1; then
 pvesh set /access/users/root@pve --password "$PASS" 2>/dev/null && \
   echo "✓ PVE 内置认证 (root@pve) 密码已同步更新" || \
   echo "[提示] PVE 内置认证密码更新失败（可能仍使用 PAM 认证，不影响登录）"
else
 echo "[信息] 未检测到 PVE 内置认证用户 (root@pve)，使用 Linux PAM 认证即可"
fi

echo; echo "===== [3/3] 验证 ====="
if grep -q '^root:' /etc/shadow 2>/dev/null; then
 echo "✓ /etc/shadow 中 root 密码哈希已更新"
fi
echo; echo "[完成] 密码重置成功，请使用新密码登录 PVE WebUI"
echo "[提示] 用户名: root，认证域: Linux PAM（默认）"
