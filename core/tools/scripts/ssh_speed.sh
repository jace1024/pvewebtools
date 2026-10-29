
SSH_CFG=/etc/ssh/sshd_config
[ -f "$SSH_CFG" ] || { echo "[错误] 未找到 $SSH_CFG"; exit 1; }
backup_file "$SSH_CFG"
set_opt() {  # $1=配置项 $2=值：已存在则原地改（sshd 取首个匹配项），不存在则追加
 if grep -qE "^[#[:space:]]*$1\b" "$SSH_CFG"; then
   sed -i "s|^[#[:space:]]*$1\b.*|$1 $2|" "$SSH_CFG"
 else
   echo "$1 $2" >> "$SSH_CFG"
 fi
 echo "[写入] $SSH_CFG [$1 $2]"
}
set_opt UseDNS no
set_opt GSSAPIAuthentication no
if ! sshd -t; then
 echo "[错误] sshd 配置校验失败，未重启服务，请手工检查 $SSH_CFG"; exit 1
fi
systemctl restart ssh 2>/dev/null || systemctl restart sshd
# 写后验证：配置确实改到位 + 服务仍在运行
if ! grep -qE "^UseDNS no" "$SSH_CFG" || ! grep -qE "^GSSAPIAuthentication no" "$SSH_CFG"; then
 echo "[错误] 配置写入未生效，请检查 $SSH_CFG"; exit 1
fi
sleep 1
# 服务名在 Debian 上可能是 ssh 或 sshd，任一在运行即可（run 会直接 exit，不能串联 ||）
if ! systemctl is-active --quiet ssh && ! systemctl is-active --quiet sshd; then
 echo "[错误] 命令执行失败: systemctl is-active ssh / sshd（服务未处于运行状态）"; exit 1
fi
echo "[完成] SSH 连接速度优化已应用（已关闭 DNS 反查与 GSSAPI 认证）"
