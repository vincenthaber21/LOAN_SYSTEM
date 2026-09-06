(function () {
  'use strict';

  var SIDEBAR_MQ = window.matchMedia('(max-width: 991.98px)');

  function setSidebar(open) {
    document.body.classList.toggle('sidebar-open', open);
    var toggle = document.querySelector('[data-sidebar-toggle]');
    if (toggle) {
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    }
  }

  function closeSidebarIfOverlay() {
    if (SIDEBAR_MQ.matches) setSidebar(false);
  }

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('[data-sidebar-toggle]');
    var backdrop = event.target.closest('.sidebar-backdrop');
    if (toggle) {
      event.preventDefault();
      setSidebar(!document.body.classList.contains('sidebar-open'));
      return;
    }
    if (backdrop) {
      setSidebar(false);
      return;
    }

    var navLink = event.target.closest('.sidebar-nav a.nav-link:not(.nav-link-toggle)');
    if (navLink) closeSidebarIfOverlay();

    var confirmButton = event.target.closest('[data-confirm]');
    if (confirmButton && !window.confirm(confirmButton.getAttribute('data-confirm'))) {
      event.preventDefault();
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      setSidebar(false);
    }
  });

  if (typeof SIDEBAR_MQ.addEventListener === 'function') {
    SIDEBAR_MQ.addEventListener('change', function (event) {
      if (!event.matches) setSidebar(false);
    });
  } else if (typeof SIDEBAR_MQ.addListener === 'function') {
    SIDEBAR_MQ.addListener(function (event) {
      if (!event.matches) setSidebar(false);
    });
  }

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches('[data-client-validation]')) return;
    if (!form.checkValidity()) {
      event.preventDefault();
      event.stopPropagation();
      form.classList.add('was-validated');
      var firstInvalid = form.querySelector(':invalid');
      if (firstInvalid) firstInvalid.focus();
    }
  });

  document.querySelectorAll('[data-auto-dismiss]').forEach(function (alert) {
    window.setTimeout(function () {
      alert.style.opacity = '0';
      window.setTimeout(function () { alert.remove(); }, 220);
    }, 6500);
  });

  document.querySelectorAll('[data-copy-value]').forEach(function (button) {
    button.addEventListener('click', function () {
      var value = button.getAttribute('data-copy-value');
      if (!navigator.clipboard || !value) return;
      navigator.clipboard.writeText(value).then(function () {
        var label = button.querySelector('[data-copy-label]');
        if (label) {
          var original = label.textContent;
          label.textContent = 'Copied';
          window.setTimeout(function () { label.textContent = original; }, 1400);
        }
      });
    });
  });

  document.querySelectorAll('[data-password-toggle]').forEach(function (button) {
    button.addEventListener('click', function () {
      var input = document.getElementById(button.getAttribute('data-target'));
      if (!input) return;
      var isPassword = input.getAttribute('type') === 'password';
      input.setAttribute('type', isPassword ? 'text' : 'password');
      button.setAttribute('aria-label', isPassword ? 'Hide password' : 'Show password');
      var icon = button.querySelector('i');
      if (icon) icon.className = isPassword ? 'bi bi-eye-slash' : 'bi bi-eye';
    });
  });
})();
