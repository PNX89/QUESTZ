(function () {
  var form = document.querySelector('[data-testid="login-form"]');
  if (!form) {
    return;
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var user = document.querySelector('[data-testid="username"]').value;
    var secret = document.querySelector('[data-testid="password"]').value;
    var error = document.querySelector('[data-testid="login-error"]');
    if (user === "demo" && secret === "demo-password") {
      sessionStorage.setItem("questz_auth", "1");
      location.assign("items.html");
      return;
    }
    error.hidden = false;
    error.textContent = "Those are not the demo credentials.";
  });
})();
