/**
 * premium-guard.js — S.N.E.T.C.H site-wide premium paywall popup.
 *
 * The backend (app.py's before_request gate) returns HTTP 402 with
 * {"error": "premium_required", ...} whenever a signed-in user without
 * the right plan calls a gated action (song download start, video
 * download start, entertainment/media download, any Astro Insights
 * session action). This script wraps window.fetch once, site-wide, so
 * NO individual feature page's JS needs to know premium exists — the
 * popup just appears automatically wherever it's needed.
 */
(function () {
  if (window.__snetchPremiumGuardInstalled) return;
  window.__snetchPremiumGuardInstalled = true;

  function ensureModal() {
    if (document.getElementById('snetch-premium-modal')) return;
    const overlay = document.createElement('div');
    overlay.id = 'snetch-premium-modal';
    overlay.style.cssText = `
      position: fixed; inset: 0; background: rgba(5,3,16,0.78); backdrop-filter: blur(6px);
      display: none; align-items: center; justify-content: center; z-index: 99999; padding: 20px;
      font-family: 'Inter', sans-serif;
    `;
    overlay.innerHTML = `
      <div style="background:#150c2e; border:1px solid rgba(168,142,255,0.35); border-radius:18px;
                  padding:28px; max-width:380px; width:100%; text-align:center; color:#e9e4ff;">
        <div style="font-size:2.2rem; color:#ffd166; margin-bottom:10px;">👑</div>
        <h3 style="margin:0 0 8px; color:#fff; font-size:1.15rem;">You don't have any plan</h3>
        <p id="snetch-premium-modal-msg" style="color:#a89bd6; font-size:0.9rem; margin:0 0 20px;">
          Premium lijiye and enjoy this feature!
        </p>
        <div style="display:flex; gap:10px;">
          <button id="snetch-premium-modal-close" style="flex:1; padding:11px; border-radius:10px; border:none;
                  background:rgba(255,255,255,0.08); color:#cfc4ff; font-weight:600; cursor:pointer;">Not now</button>
          <button id="snetch-premium-modal-go" style="flex:1; padding:11px; border-radius:10px; border:none;
                  background:linear-gradient(135deg,#8a6cff,#6a3ff5); color:#fff; font-weight:600; cursor:pointer;">
            View Plans
          </button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('#snetch-premium-modal-close').addEventListener('click', () => {
      overlay.style.display = 'none';
    });
    overlay.querySelector('#snetch-premium-modal-go').addEventListener('click', () => {
      window.location.href = '/premium';
    });
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.style.display = 'none';
    });
  }

  function showPremiumPopup(message) {
    ensureModal();
    const overlay = document.getElementById('snetch-premium-modal');
    const msgEl = document.getElementById('snetch-premium-modal-msg');
    if (msgEl && message) msgEl.textContent = message;
    overlay.style.display = 'flex';
  }

  const originalFetch = window.fetch;
  window.fetch = function (...args) {
    return originalFetch.apply(this, args).then((response) => {
      if (response.status === 402) {
        response
          .clone()
          .json()
          .then((data) => {
            if (data && data.error === 'premium_required') {
              showPremiumPopup(data.message || "You don't have any plan. Premium lijiye and enjoy this feature!");
            }
          })
          .catch(() => {});
      }
      return response;
    });
  };
})();
