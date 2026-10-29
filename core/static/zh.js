// 中文用户可见文案集中管理（重构 T4.4，先中文单语言）。
// 与 core/web/messages.py 保持同步；未来可扩展为多语言字典。
// 后端 t(key) 与此处 window.t(key) 使用同一套 key。
window.MESSAGES = {
  // ---- 授权域 ----
  "lic_added": "已添加授权：%s（%s）",
  "lic_added_no_match": "已添加授权（指纹 %s），但当前集群里没有指纹与之相符的机器 —— 熔断不会因此解除。请确认发给授权方的指纹就是该机器的指纹，并确认该机器当前可达。",
  "lic_need_ip_pwd": "请填写机器 IP 与该机器 root 密码",
  "lic_host_format": "机器地址格式非法（仅允许字母数字及 .-:[] ）",
  "lic_need_ip": "请填写机器 IP（不支持主机名，避免与本机或现有节点重名）",
  "lic_no_ssh": "本机缺少 ssh 与 setsid，无法用密码连接新机器",
  "lic_fp_fail": "无法采集该机器硬件指纹（虚机节点不支持、或不可达）",
  // ---- 错误映射 ----
  "err_internal": "服务器内部错误",
};

window.t = function (key) {
  var v = window.MESSAGES[key];
  return v !== undefined ? v : key;
};
