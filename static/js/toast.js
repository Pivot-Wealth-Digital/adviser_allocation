/**
 * Shared toast notification utility.
 * Reuses .msg, .ok, .err, .warn CSS classes from app.css.
 * Usage: Toast.ok('Saved!'), Toast.err('Failed'), Toast.warn('Check input')
 */
(function () {
  var DISMISS_MS = 2200;

  function show(type, text) {
    var el = document.createElement('div');
    el.className = 'msg ' + type;
    el.textContent = text;
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0';
      el.style.transition = 'opacity 0.2s';
      setTimeout(function () { el.remove(); }, 200);
    }, DISMISS_MS);
  }

  window.Toast = {
    ok: function (text) { show('ok', text); },
    err: function (text) { show('err', text || 'Something went wrong'); },
    warn: function (text) { show('warn', text); }
  };
})();
