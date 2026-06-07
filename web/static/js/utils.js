/* ============================================================
   utils.js — DOM helpers, formatting, small utilities
   LoveChoice Voice Console
   ============================================================ */

/* ---- DOM query ---- */

export function $(selector) {
  return document.querySelector(selector);
}

/* ---- get/set form values ---- */

export function setValue(id, value) {
  const el = document.getElementById(id);
  if (el) el.value = value;
}

export function value(id, fallback = "") {
  const el = document.getElementById(id);
  return el ? el.value : fallback;
}

export function setChecked(id, checked) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(checked);
}

export function checked(id, fallback = false) {
  const el = document.getElementById(id);
  return el ? Boolean(el.checked) : fallback;
}

export function setPlaceholder(id, text) {
  const el = document.getElementById(id);
  if (el && text) el.placeholder = text;
}

/* ---- text / content ---- */

export function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

/* ---- icon rendering ---- */

export function renderIcons() {
  if (window.lucide) window.lucide.createIcons();
}

/* ---- conversation meta formatting ---- */

export function formatConversationMeta(conversation) {
  const sequence = conversation.sequence ? `第 ${conversation.sequence} 次` : "本次";
  const count = Number(conversation.message_count || 0);
  return `${sequence} · ${count} 条`;
}

/* ---- toast notification ---- */

let toastContainer = null;

function ensureToastContainer() {
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-container";
    document.body.appendChild(toastContainer);
  }
  return toastContainer;
}

export function showToast(message, type = "info") {
  ensureToastContainer();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(40px)";
    toast.style.transition = "all 240ms ease-out";
    setTimeout(() => toast.remove(), 260);
  }, 3800);
}

/* ---- modal confirmation (replaces window.confirm) ---- */

export function showConfirm(message) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "confirm-overlay";
    overlay.innerHTML = `
      <div class="confirm-dialog">
        <p>${message}</p>
        <div class="confirm-actions">
          <button class="secondary-action confirm-cancel">取消</button>
          <button class="primary-action confirm-ok">确认</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    overlay.querySelector(".confirm-cancel").onclick = () => { overlay.remove(); resolve(false); };
    overlay.querySelector(".confirm-ok").onclick = () => { overlay.remove(); resolve(true); };
    overlay.addEventListener("click", (e) => { if (e.target === overlay) { overlay.remove(); resolve(false); } });
  });
}

/* ---- skeleton helpers ---- */

export function showSkeleton(containerId, count = 4) {
  const host = document.getElementById(containerId);
  if (!host) return;
  host.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const div = document.createElement("div");
    div.className = "skeleton";
    div.style.cssText = `height:52px;margin-bottom:8px;`;
    host.appendChild(div);
  }
}
