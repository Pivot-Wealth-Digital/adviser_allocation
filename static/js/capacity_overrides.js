(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const submitBtn = $('#submit');
  const table = $('#override-table');

  const podOptions = Array.isArray(window.POD_TYPE_OPTIONS) ? window.POD_TYPE_OPTIONS : [];
  const adviserOptions = Array.isArray(window.ADVISER_OPTIONS) ? window.ADVISER_OPTIONS : [];

  const escapeHtml = (value = '') =>
    String(value)
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

  function buildPodSelect(value = '') {
    const known = podOptions.slice();
    if (value && !known.includes(value)) {
      known.push(value);
    }
    const options = ['<option value="">-- Optional --</option>'];
    known.forEach(pod => {
      const podEsc = escapeHtml(pod);
      const selected = pod === value ? ' selected' : '';
      options.push(`<option value="${podEsc}"${selected}>${podEsc}</option>`);
    });
    return `<select class="pod-select">${options.join('')}</select>`;
  }

  function buildAdviserSelect(selectedEmail = '') {
    const options = ['<option value="">-- Select adviser --</option>'];
    adviserOptions.forEach(opt => {
      const emailEsc = escapeHtml(opt.email || '');
      const labelEsc = escapeHtml(opt.label || opt.email || '');
      const selected = (opt.email || '') === selectedEmail ? ' selected' : '';
      options.push(`<option value="${emailEsc}"${selected}>${labelEsc}</option>`);
    });
    return `<select class="email-select">${options.join('')}</select>`;
  }

  async function postOverride(payload) {
    const res = await fetch('/capacity_overrides', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(payload)
    });
    if (res.status === 401) {
      window.location.href = '/login?next=/capacity_overrides/ui';
      return null;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || 'Failed to create override');
    }
    return data;
  }

  async function putOverride(id, payload) {
    const res = await fetch(`/capacity_overrides/${id}`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(payload)
    });
    if (res.status === 401) {
      window.location.href = '/login?next=/capacity_overrides/ui';
      return null;
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || 'Failed to update override');
    }
    return data;
  }

  async function deleteOverride(id) {
    const res = await fetch(`/capacity_overrides/${id}`, {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    });
    if (res.status === 401) {
      window.location.href = '/login?next=/capacity_overrides/ui';
      return false;
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || 'Failed to delete override');
    }
    return true;
  }

  function resetRow(tr, original) {
    tr.innerHTML = original;
  }

  if (submitBtn) {
    submitBtn.addEventListener('click', async () => {
      const emailSelect = $('#emailSelect');
      const email = (emailSelect && emailSelect.value) || '';
      const effective = ($('#effective') || {}).value || '';
      const limit = ($('#limit') || {}).value || '';
      const podType = ($('#podType') || {}).value || '';
      const notes = ($('#notes') || {}).value || '';

      if (!email.trim()) {
        Toast.err('Adviser email is required.');
        return;
      }
      if (!effective.trim()) {
        Toast.err('Effective date is required.');
        return;
      }
      if (!limit.trim()) {
        Toast.err('Monthly limit is required.');
        return;
      }

      const payload = {
        adviser_email: email.trim().toLowerCase(),
        effective_date: effective.trim(),
        client_limit_monthly: Number(limit),
        pod_type: podType.trim(),
        notes: notes.trim()
      };

      try {
        await postOverride(payload);
        Toast.ok('Override added.');
        setTimeout(() => window.location.reload(), 500);
      } catch (err) {
        Toast.err(err.message);
      }
    });
  }

  if (table) {
    table.addEventListener('click', async (event) => {
      const btn = event.target;
      const tr = btn.closest ? btn.closest('tr') : null;
      if (!tr) return;
      const id = tr.dataset.id;
      if (!id) return;

      if (btn.classList.contains('edit')) {
        const emailVal = (tr.querySelector('.email') || {}).textContent || '';
        const effectiveVal = (tr.querySelector('.effective') || {}).textContent || '';
        const limitVal = (tr.querySelector('.limit') || {}).textContent || '';
        const podTypeVal = (tr.querySelector('.pod_type') || {}).textContent || '';
        const notesVal = (tr.querySelector('.notes') || {}).textContent || '';

        tr.dataset.original = tr.innerHTML;

        tr.querySelector('.email').innerHTML = buildAdviserSelect(emailVal.trim());
        tr.querySelector('.effective').innerHTML = `<input type="date" class="effective-input" value="${effectiveVal.trim()}" />`;
        tr.querySelector('.limit').innerHTML = `<input type="number" class="limit-input" min="1" value="${limitVal.trim()}" />`;
        tr.querySelector('.pod_type').innerHTML = buildPodSelect(podTypeVal.trim());
        const podSelect = tr.querySelector('.pod-select');
        if (podSelect) {
          podSelect.value = podTypeVal.trim();
        }
        tr.querySelector('.notes').innerHTML = `<input type="text" class="notes-input" value="${notesVal.trim()}" />`;

        tr.querySelector('.edit').style.display = 'none';
        tr.querySelector('.delete').style.display = 'none';
        tr.querySelector('.save').style.display = 'inline-block';
        tr.querySelector('.cancel').style.display = 'inline-block';
        return;
      }

      if (btn.classList.contains('cancel')) {
        if (tr.dataset.original) {
          tr.innerHTML = tr.dataset.original;
        } else {
          window.location.reload();
        }
        return;
      }

      if (btn.classList.contains('save')) {
        const emailSelect = tr.querySelector('.email-select');
        const effectiveInput = tr.querySelector('.effective-input');
        const limitInput = tr.querySelector('.limit-input');
        const podInput = tr.querySelector('.pod-select');
        const notesInput = tr.querySelector('.notes-input');

        const payload = {};

        if (emailSelect) {
          const emailValue = emailSelect.value.trim().toLowerCase();
          if (!emailValue) {
            Toast.err('Adviser email cannot be blank.');
            return;
          }
          payload.adviser_email = emailValue;
        }

        if (effectiveInput) {
          const effectiveValue = effectiveInput.value.trim();
          if (!effectiveValue) {
            Toast.err('Effective date cannot be blank.');
            return;
          }
          payload.effective_date = effectiveValue;
        }

        if (limitInput) {
          const limitValue = limitInput.value.trim();
          if (!limitValue) {
            Toast.err('Monthly limit cannot be blank.');
            return;
          }
          payload.client_limit_monthly = Number(limitValue);
        }

        if (podInput) {
          payload.pod_type = podInput.value.trim();
        }

        if (notesInput) {
          payload.notes = notesInput.value.trim();
        }

        try {
          await putOverride(id, payload);
          Toast.ok('Override updated.');
          setTimeout(() => window.location.reload(), 500);
        } catch (err) {
          Toast.err(err.message);
        }
        return;
      }

      if (btn.classList.contains('delete')) {
        if (!window.confirm('Delete this override?')) return;
        try {
          const success = await deleteOverride(id);
          if (success) {
            Toast.ok('Override deleted.');
            setTimeout(() => window.location.reload(), 500);
          }
        } catch (err) {
          Toast.err(err.message);
        }
      }
    });
  }
})();
