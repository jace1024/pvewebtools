// 中文用户可见文案集中管理（先中文单语言）。
// 与 core/web/messages.py 保持同步；未来可扩展为多语言字典。
// 后端 t(key) 与此处 window.t(key) 使用同一套 key。
window.MESSAGES = {
  // ---- 错误映射 ----
  "err_internal": "服务器内部错误",
};

window.t = function (key) {
  var v = window.MESSAGES[key];
  return v !== undefined ? v : key;
};
