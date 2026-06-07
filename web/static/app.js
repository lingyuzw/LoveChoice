/* ============================================================
   app.js — Backward-compatible re-export shim

   原 app.js (1614 行) 已拆分为以下模块：
     js/state.js      — 全局状态 & 默认常量
     js/utils.js      — DOM 工具 / 格式化 / Toast
     js/api.js        — 后端 API 调用
     js/audio.js      — 音频采集 / PCM 播放 / 电平
     js/dialog.js     — WebSocket / 对话事件 / 打断
     js/ui-dashboard.js — 对话页 UI
     js/ui-services.js  — 服务页 UI
     js/ui-settings.js  — 配置页 UI
     js/main.js       — 入口 (页面路由)

   新入口为 <script type="module" src="/static/js/main.js">
   本文件仅保留向后兼容，不包含任何实际逻辑。
   ============================================================ */

console.log(
  "%cLoveChoice Voice Console%c\n前端模块已升级。入口: /static/js/main.js",
  "color:#0f766e;font-weight:900;font-size:14px;",
  "color:#6b7280;"
);
