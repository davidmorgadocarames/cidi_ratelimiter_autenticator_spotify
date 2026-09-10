import { authFetch } from "./app-state.js";
import {
  NETWORK_ERROR_MESSAGE,
  withLoading,
  extractErrorMessage,
  showError,
  clearError,
} from "./shared.js";

// deps: { onUserUpdated, logout } - onUserUpdated(user) reevalúa el gate
// (routeAfterAuth, en app.js) cada vez que cambia is_premium/totp_enabled,
// para que confirmar 2FA o activar premium avance automáticamente a la
// Pantalla 3 si ambas condiciones ya se cumplen.
export function initScreen2({ onUserUpdated, logout }) {
  const dashboardEmail = document.getElementById("dashboard-email");
  const backToAppBtn = document.getElementById("back-to-app");

  const totpStatus = document.getElementById("totp-status");
  const totpSetupStart = document.getElementById("totp-setup-start");
  const totpSetupPassword = document.getElementById("totp-setup-password");
  const totpSetupError = document.getElementById("totp-setup-error");
  const totpSetupButton = document.getElementById("totp-setup-button");
  const totpConfirmSection = document.getElementById("totp-confirm-section");
  const totpQr = document.getElementById("totp-qr");
  const totpSecret = document.getElementById("totp-secret");
  const totpConfirmCode = document.getElementById("totp-confirm-code");
  const totpConfirmError = document.getElementById("totp-confirm-error");
  const totpConfirmButton = document.getElementById("totp-confirm-button");

  const premiumStatus = document.getElementById("premium-status");
  const dashboardError = document.getElementById("dashboard-error");
  const togglePremiumBtn = document.getElementById("toggle-premium");
  const premiumNeeds2faHint = document.getElementById("premium-needs-2fa-hint");
  const premiumActivateForm = document.getElementById("premium-activate-form");
  const premiumPassword = document.getElementById("premium-password");
  const premiumTotpCode = document.getElementById("premium-totp-code");
  const premiumActivateError = document.getElementById("premium-activate-error");
  const premiumActivateConfirm = document.getElementById("premium-activate-confirm");
  const premiumActivateCancel = document.getElementById("premium-activate-cancel");

  const logoutBtn = document.getElementById("logout");

  let currentUser = null;

  function render(user) {
    currentUser = user;
    dashboardEmail.textContent = user.email;
    // Solo tiene sentido mostrarlo si el usuario YA cumple el gate (llegó
    // aquí desde la Pantalla 3 vía "Seguridad y Premium", no porque nunca
    // haya calificado) - sin esto, la Pantalla 2 era un callejón sin salida
    // para un usuario premium+2FA que solo quería revisar su seguridad
    // (hallazgo real de la revisión "abogado del diablo").
    backToAppBtn.hidden = !(user.is_premium && user.totp_enabled);

    totpStatus.textContent = user.totp_enabled ? "Activado" : "No configurado";
    totpSetupStart.hidden = user.totp_enabled;
    totpConfirmSection.hidden = true;
    clearError(totpSetupError);
    clearError(totpConfirmError);
    totpSetupPassword.value = "";
    totpConfirmCode.value = "";

    premiumStatus.textContent = user.is_premium ? "Sí" : "No";
    togglePremiumBtn.textContent = user.is_premium ? "Desactivar premium" : "Activar premium";
    delete togglePremiumBtn.dataset.loading;
    premiumNeeds2faHint.hidden = true;
    premiumActivateForm.hidden = true;
    premiumPassword.value = "";
    premiumTotpCode.value = "";
    clearError(premiumActivateError);
    clearError(dashboardError);
  }

  totpSetupButton.addEventListener("click", async () => {
    clearError(totpSetupError);
    await withLoading(totpSetupButton, "Configurando…", async () => {
      try {
        const response = await authFetch("/2fa/setup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: totpSetupPassword.value }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(totpSetupError, extractErrorMessage(data, "No se pudo configurar 2FA"));
          totpSetupPassword.focus();
          return;
        }
        const data = await response.json();
        totpQr.src = `data:image/png;base64,${data.qr_code_base64}`;
        totpSecret.textContent = data.secret;
        totpConfirmSection.hidden = false;
        totpConfirmCode.focus();
      } catch {
        showError(totpSetupError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  totpConfirmButton.addEventListener("click", async () => {
    clearError(totpConfirmError);
    await withLoading(totpConfirmButton, "Confirmando…", async () => {
      try {
        const response = await authFetch("/2fa/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: totpConfirmCode.value }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(totpConfirmError, extractErrorMessage(data, "No se pudo confirmar el código"));
          totpConfirmCode.focus();
          return;
        }
        onUserUpdated();
      } catch {
        showError(totpConfirmError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  togglePremiumBtn.addEventListener("click", async () => {
    clearError(dashboardError);

    if (currentUser.is_premium) {
      await withLoading(togglePremiumBtn, "Guardando…", async () => {
        try {
          const response = await authFetch("/users/me/premium/deactivate", { method: "POST" });
          if (!response.ok) {
            showError(dashboardError, "No se pudo desactivar premium.");
            return;
          }
          onUserUpdated();
        } catch {
          showError(dashboardError, NETWORK_ERROR_MESSAGE);
        }
      });
      return;
    }

    if (!currentUser.totp_enabled) {
      premiumNeeds2faHint.hidden = false;
      premiumActivateForm.hidden = true;
      return;
    }

    premiumNeeds2faHint.hidden = true;
    premiumActivateForm.hidden = false;
    premiumPassword.focus();
  });

  premiumActivateCancel.addEventListener("click", () => {
    premiumActivateForm.hidden = true;
    premiumPassword.value = "";
    premiumTotpCode.value = "";
    clearError(premiumActivateError);
  });

  premiumActivateConfirm.addEventListener("click", async () => {
    clearError(premiumActivateError);
    await withLoading(premiumActivateConfirm, "Activando…", async () => {
      try {
        const response = await authFetch("/users/me/premium/activate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            password: premiumPassword.value,
            totp_code: premiumTotpCode.value,
          }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(premiumActivateError, extractErrorMessage(data, "No se pudo activar premium"));
          premiumTotpCode.focus();
          return;
        }
        onUserUpdated();
      } catch {
        showError(premiumActivateError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  logoutBtn.addEventListener("click", () => withLoading(logoutBtn, "Cerrando sesión…", logout));

  // Reutiliza el mismo callback que ya reevalúa el gate tras confirmar TOTP
  // o activar/desactivar premium (onUserUpdated -> refreshUserAndRoute en
  // app.js) - "volver" es solo "vuelve a comprobar si califico".
  backToAppBtn.addEventListener("click", onUserUpdated);

  return { render };
}
