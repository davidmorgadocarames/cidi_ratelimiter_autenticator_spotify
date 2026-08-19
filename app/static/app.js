(() => {
  const NETWORK_ERROR_MESSAGE = "Error de red. Inténtalo de nuevo.";

  const state = { accessToken: null };
  let currentUser = null;

  const loadingView = document.getElementById("loading-view");
  const authView = document.getElementById("auth-view");
  const dashboardView = document.getElementById("dashboard-view");

  const loginSection = document.getElementById("login-section");
  const registerSection = document.getElementById("register-section");

  const loginForm = document.getElementById("login-form");
  const loginEmail = document.getElementById("login-email");
  const loginPassword = document.getElementById("login-password");
  const loginError = document.getElementById("login-error");
  const loginSubmit = document.getElementById("login-submit");

  const registerForm = document.getElementById("register-form");
  const registerEmail = document.getElementById("register-email");
  const registerPassword = document.getElementById("register-password");
  const registerError = document.getElementById("register-error");
  const registerSubmit = document.getElementById("register-submit");

  const showRegisterBtn = document.getElementById("show-register");
  const showLoginBtn = document.getElementById("show-login");

  const dashboardEmail = document.getElementById("dashboard-email");

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

  function showView(view) {
    loadingView.hidden = view !== "loading";
    authView.hidden = view !== "auth";
    dashboardView.hidden = view !== "dashboard";
  }

  function showError(el, message) {
    el.textContent = message;
    el.hidden = false;
  }

  function clearError(el) {
    el.textContent = "";
    el.hidden = true;
  }

  // FastAPI/Pydantic devuelve `detail` como string en errores "de negocio"
  // (HTTPException) pero como una LISTA de objetos {loc, msg, type} en errores
  // 422 de validación de body. Sin esto, `data.detail || fallback` asigna el
  // array a textContent y el usuario ve literalmente "[object Object]".
  function extractErrorMessage(data, fallback) {
    const detail = data && data.detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
    if (Array.isArray(detail) && detail.length > 0 && typeof detail[0]?.msg === "string") {
      return detail[0].msg;
    }
    return fallback;
  }

  // button.dataset.loading marca explícitamente "este botón sigue mostrando su
  // label de carga"; si algo (ej. renderDashboard) ya puso el label definitivo
  // durante fn(), lo borra para que el finally de abajo no lo pise de vuelta.
  async function withLoading(button, labelWhileLoading, fn) {
    const originalLabel = button.textContent;
    button.disabled = true;
    if (labelWhileLoading) {
      button.textContent = labelWhileLoading;
      button.dataset.loading = "true";
    }
    try {
      await fn();
    } finally {
      button.disabled = false;
      if (button.dataset.loading === "true") {
        button.textContent = originalLabel;
      }
      delete button.dataset.loading;
    }
  }

  function authFetch(url, options = {}) {
    const headers = { ...(options.headers || {}), Authorization: `Bearer ${state.accessToken}` };
    return fetch(url, { ...options, headers });
  }

  function renderDashboard(user) {
    currentUser = user;
    dashboardEmail.textContent = user.email;

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

    showView("dashboard");
  }

  async function fetchMe() {
    const response = await authFetch("/auth/me");
    if (!response.ok) throw new Error("No se pudo obtener el usuario actual");
    return response.json();
  }

  async function trySilentRefresh() {
    try {
      const response = await fetch("/auth/refresh", { method: "POST" });
      if (!response.ok) {
        showView("auth");
        return;
      }
      const data = await response.json();
      state.accessToken = data.access_token;
      const user = await fetchMe();
      renderDashboard(user);
    } catch {
      showView("auth");
    }
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError(loginError);
    await withLoading(loginSubmit, "Iniciando sesión…", async () => {
      try {
        const body = new URLSearchParams();
        body.set("username", loginEmail.value);
        body.set("password", loginPassword.value);
        const response = await fetch("/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(loginError, extractErrorMessage(data, "No se pudo iniciar sesión"));
          loginPassword.focus();
          return;
        }
        const data = await response.json();
        state.accessToken = data.access_token;
        const user = await fetchMe();
        renderDashboard(user);
      } catch {
        showError(loginError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  registerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearError(registerError);
    await withLoading(registerSubmit, "Creando cuenta…", async () => {
      try {
        const response = await fetch("/auth/register", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: registerEmail.value, password: registerPassword.value }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          showError(registerError, extractErrorMessage(data, "No se pudo crear la cuenta"));
          registerEmail.focus();
          return;
        }

        // Auto-login con las mismas credenciales tras un registro exitoso.
        const loginBody = new URLSearchParams();
        loginBody.set("username", registerEmail.value);
        loginBody.set("password", registerPassword.value);
        const loginResponse = await fetch("/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: loginBody,
        });
        if (!loginResponse.ok) {
          showError(registerError, "Cuenta creada. Inicia sesión manualmente.");
          return;
        }
        const data = await loginResponse.json();
        state.accessToken = data.access_token;
        const user = await fetchMe();
        renderDashboard(user);
      } catch {
        showError(registerError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  showRegisterBtn.addEventListener("click", () => {
    clearError(loginError);
    registerEmail.value = loginEmail.value;
    loginSection.hidden = true;
    registerSection.hidden = false;
    registerEmail.focus();
  });

  showLoginBtn.addEventListener("click", () => {
    clearError(registerError);
    loginEmail.value = registerEmail.value;
    registerSection.hidden = true;
    loginSection.hidden = false;
    loginEmail.focus();
  });

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
        const user = await fetchMe();
        renderDashboard(user);
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
          const user = await response.json();
          renderDashboard(user);
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
        const user = await response.json();
        renderDashboard(user);
      } catch {
        showError(premiumActivateError, NETWORK_ERROR_MESSAGE);
      }
    });
  });

  logoutBtn.addEventListener("click", async () => {
    await withLoading(logoutBtn, "Cerrando sesión…", async () => {
      try {
        await fetch("/auth/logout", { method: "POST" });
      } catch {
        // Best-effort: aunque falle la llamada de red, seguimos cerrando la
        // sesión en el cliente (el refresh token en el servidor expira solo).
      }
      state.accessToken = null;
      currentUser = null;
      loginForm.reset();
      registerForm.reset();
      showView("auth");
    });
  });

  trySilentRefresh();
})();
