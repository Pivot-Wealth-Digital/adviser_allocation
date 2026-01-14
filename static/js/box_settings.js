(function() {
  const templatePathInput = document.getElementById('template-path');
  const notesInput = document.getElementById('notes');
  const testButton = document.getElementById('test-path');
  const saveButton = document.getElementById('save-settings');
  const testResult = document.getElementById('test-result');
  const toastOk = document.getElementById('toast-ok');
  const toastErr = document.getElementById('toast-err');

  function showToast(el, text) {
    el.textContent = text;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 3000);
  }

  function showTestResult(message, isSuccess) {
    testResult.textContent = message;
    testResult.style.display = 'block';
    testResult.style.background = isSuccess ? '#d4edda' : '#f8d7da';
    testResult.style.color = isSuccess ? '#155724' : '#721c24';
    testResult.style.border = `1px solid ${isSuccess ? '#c3e6cb' : '#f5c6cb'}`;
  }

  testButton.addEventListener('click', async function() {
    const path = templatePathInput.value.trim();
    if (!path) {
      showTestResult('❌ Please enter a path to test', false);
      return;
    }

    testButton.disabled = true;
    testButton.textContent = 'Testing...';
    testResult.style.display = 'none';

    try {
      const response = await fetch('/settings/box/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ path: path })
      });

      const data = await response.json();

      if (response.status === 401) {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
        return;
      }

      if (data.valid) {
        showTestResult(data.message, true);
      } else {
        showTestResult(data.message || data.error, false);
      }
    } catch (err) {
      showTestResult('❌ Test failed: ' + err.message, false);
    } finally {
      testButton.disabled = false;
      testButton.textContent = 'Test Path';
    }
  });

  saveButton.addEventListener('click', async function() {
    const path = templatePathInput.value.trim();
    const notes = notesInput.value.trim();

    if (!path) {
      showToast(toastErr, 'Template folder path is required');
      return;
    }

    if (!confirm(`Update Box template path to:\n"${path}"\n\nThis will affect all new Box folder creations. Continue?`)) {
      return;
    }

    saveButton.disabled = true;
    saveButton.textContent = 'Saving...';

    try {
      const response = await fetch('/settings/box', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          template_folder_path: path,
          notes: notes
        })
      });

      if (response.status === 401) {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
        return;
      }

      const data = await response.json();

      if (response.ok) {
        showToast(toastOk, '✅ Box settings saved successfully');
        setTimeout(() => window.location.reload(), 1500);
      } else {
        showToast(toastErr, data.error || 'Failed to save settings');
      }
    } catch (err) {
      showToast(toastErr, 'Error: ' + err.message);
    } finally {
      saveButton.disabled = false;
      saveButton.textContent = 'Save Settings';
    }
  });
})();
