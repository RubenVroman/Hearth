const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");

["email", "password"].forEach((id) => {
  const field = document.getElementById(id);
  if (!field) return;
  field.addEventListener("focus", () => {
    field.scrollIntoView({ block: "center", inline: "nearest" });
  });
});

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  errorEl.classList.add("hidden");
  errorEl.textContent = "";
  const email = document.getElementById("email").value.trim();
  const password = document.getElementById("password").value;
  try {
    const response = await fetch("/auth/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!response.ok) {
      errorEl.textContent = "That login did not work.";
      errorEl.classList.remove("hidden");
      return;
    }
    window.location.replace("/");
  } catch (_) {
    errorEl.textContent = "Could not reach Hearth.";
    errorEl.classList.remove("hidden");
  }
});
