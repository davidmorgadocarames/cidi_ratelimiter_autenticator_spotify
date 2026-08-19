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
  const premiumStatus = document.getElementById("premium-status");
  const dashboardError = document.getElementById("dashboard-error");
  const togglePremiumBtn = document.getElementById("toggle-premium");
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

  function renderDashboard(user) {
    currentUser = user;
    dashboardEmail.textContent = user.email;
    premiumStatus.textContent = user.is_premium ? "Sí" : "No";
    togglePremiumBtn.textContent = user.is_premium ? "Desactivar premium" : "Activar premium";
    delete togglePremiumBtn.dataset.loading;
    showView("dashboard");
  }

  async function fetchMe() {
    const response = await fetch("/auth/me", {
      headers: { Authorization: `Bearer ${state.accessToken}` },
    });
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
          showError(loginError, data.detail || "No se pudo iniciar sesión");
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
          showError(registerError, data.detail || "No se pudo crear la cuenta");
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

  togglePremiumBtn.addEventListener("click", async () => {
    clearError(dashboardError);
    await withLoading(togglePremiumBtn, "Guardando…", async () => {
      try {
        const response = await fetch("/users/me/premium", {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${state.accessToken}`,
          },
          body: JSON.stringify({ is_premium: !currentUser.is_premium }),
        });
        if (!response.ok) {
          showError(dashboardError, "No se pudo actualizar el estado premium.");
          return;
        }
        const user = await response.json();
        renderDashboard(user);
      } catch {
        showError(dashboardError, NETWORK_ERROR_MESSAGE);
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
      clearError(dashboardError);
      showView("auth");
    });
  });

  trySilentRefresh();
})();
