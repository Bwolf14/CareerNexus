// Admin AI-settings page: option-matrix greying, per-slot connection test,
// local-model download streaming, and load.

(function () {
  // --- Option matrix: grey a value box when its checkbox is off ------------
  function syncValbox(cb) {
    const id = cb.dataset.valbox;
    if (!id) return;
    const wrap = document.getElementById('wrap_' + id);
    if (wrap) wrap.classList.toggle('off', !cb.checked);
  }
  document.querySelectorAll('.opt-on[data-valbox]').forEach(cb => {
    syncValbox(cb);
    cb.addEventListener('change', () => syncValbox(cb));
  });

  // --- Only one slot may use the bundled local model -----------------------
  const localToggles = Array.from(document.querySelectorAll('.use-local'));
  function syncLocal() {
    localToggles.forEach(t => {
      const card = t.closest('.slotcard');
      const base = card.querySelector('.slot-base');
      if (base) {
        base.disabled = t.checked;
        base.classList.toggle('off', t.checked);
      }
    });
  }
  localToggles.forEach(t => {
    t.addEventListener('change', () => {
      if (t.checked) localToggles.forEach(o => { if (o !== t) o.checked = false; });
      syncLocal();
    });
  });
  syncLocal();

  // --- Per-slot "Test connection" -----------------------------------------
  document.querySelectorAll('.test-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('.slotcard');
      const useLocal = card.querySelector('.use-local').checked;
      const base = card.querySelector('.slot-base').value;
      const result = card.querySelector('.test-result');
      const spinner = card.querySelector('.slot-spinner');
      const modelInput = card.querySelector('.slot-model');
      const datalist = card.querySelector('datalist');
      result.innerHTML = '';
      datalist.innerHTML = '';
      btn.disabled = true;
      spinner.style.display = 'inline-block';
      try {
        const resp = await fetch('/api/ai/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ base_url: base, local: useLocal }),
        });
        const data = await resp.json();
        if (data.ok) {
          if (data.normalized_url && !useLocal) {
            card.querySelector('.slot-base').value = data.normalized_url;
          }
          const models = data.models || [];
          models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            datalist.appendChild(opt);
          });
          const chips = models.slice(0, 12).map(m =>
            `<button type="button" class="btn ghost model-chip" style="padding:.25rem .6rem;font-size:.78rem;">${m}</button>`
          ).join(' ');
          result.innerHTML =
            `<div class="banner info" style="margin-top:.2rem;"><strong>Connected</strong> in ${data.latency_ms} ms — ` +
            `${models.length} model${models.length === 1 ? '' : 's'}.` +
            (chips ? `<div style="margin-top:.5rem;display:flex;gap:.3rem;flex-wrap:wrap;">${chips}</div>` : '') +
            `</div>`;
          result.querySelectorAll('.model-chip').forEach(chip =>
            chip.addEventListener('click', () => { modelInput.value = chip.textContent; }));
        } else {
          result.innerHTML = `<div class="banner error" style="margin-top:.2rem;">${data.error}</div>`;
        }
      } catch (err) {
        result.innerHTML = `<div class="banner error" style="margin-top:.2rem;">${err}</div>`;
      } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
      }
    });
  });

  // --- Local model download (streaming NDJSON progress) --------------------
  function fmtBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + u[i];
  }

  document.querySelectorAll('.pull-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const model = btn.dataset.model;
      const row = btn.closest('td');
      const prog = row.querySelector('.pull-progress');
      const fill = prog.querySelector('.fill');
      const stat = prog.querySelector('.pstat');
      prog.style.display = 'block';
      stat.style.color = '';
      fill.style.width = '0%';
      btn.disabled = true;

      // Ollama streams PER-LAYER progress (each layer its own total/completed),
      // so naive rendering makes the bar jump back to 0 on every layer and the
      // speed spike wildly. Aggregate across layers, smooth the speed with an
      // EMA, keep the bar monotonic, and throttle DOM writes.
      const layers = new Map();       // digest -> {completed, total}
      let phase = 'starting…';
      let maxPct = 0;
      let emaBps = null;
      let lastSample = { bytes: 0, time: Date.now() };
      let lastRender = 0;
      let finished = false, failed = null;

      function totals() {
        let done = 0, total = 0;
        layers.forEach(l => { done += l.completed || 0; total += l.total || 0; });
        return { done, total };
      }

      function render(force) {
        const now = Date.now();
        if (!force && now - lastRender < 200) return;   // ≤5 updates/sec
        lastRender = now;
        const { done, total } = totals();
        if (total > 0) {
          const pct = Math.min(100, Math.floor((done / total) * 100));
          maxPct = Math.max(maxPct, pct);               // never move backwards
          fill.style.width = maxPct + '%';
          const dt = (now - lastSample.time) / 1000;
          if (dt >= 0.5) {
            const bps = Math.max(0, (done - lastSample.bytes) / dt);
            emaBps = emaBps === null ? bps : (0.3 * bps + 0.7 * emaBps);
            lastSample = { bytes: done, time: now };
          }
          const speed = emaBps !== null && emaBps > 1 ? ' · ' + fmtBytes(emaBps) + '/s' : '';
          stat.textContent = maxPct + '% (' + fmtBytes(done) + ' / ' + fmtBytes(total) + ')'
                             + speed + ' — ' + phase;
        } else {
          stat.textContent = phase;
        }
      }

      try {
        const resp = await fetch('/api/models/pull', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();
          for (const line of lines) {
            if (!line.trim()) continue;
            let msg;
            try { msg = JSON.parse(line); } catch (e) { continue; }
            if (msg.error) { failed = msg.error; continue; }
            if (msg.status) phase = msg.status;
            if (msg.digest && msg.total) {
              layers.set(msg.digest, {
                completed: msg.completed || 0,
                total: msg.total,
              });
            }
            if (msg.status === 'success') finished = true;
          }
          render(false);
        }
        render(true);
        if (failed) {
          stat.textContent = 'Error: ' + failed;
          stat.style.color = 'var(--danger)';
        } else if (finished) {
          fill.style.width = '100%';
          stat.textContent = 'Downloaded ✓ — reload the page to see it as installed.';
        }
      } catch (err) {
        stat.textContent = 'Download failed: ' + err;
        stat.style.color = 'var(--danger)';
      } finally {
        btn.disabled = false;
      }
    });
  });

  // --- Load (warm) a local model ------------------------------------------
  document.querySelectorAll('.load-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const model = btn.dataset.model;
      const row = btn.closest('td');
      const stat = row.querySelector('.pstat');
      const prog = row.querySelector('.pull-progress');
      prog.style.display = 'block';
      stat.style.color = '';
      stat.textContent = 'Loading model into memory…';
      btn.disabled = true;
      try {
        const resp = await fetch('/api/models/load', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const data = await resp.json();
        stat.textContent = data.ok ? 'Loaded and ready ✓' : ('Could not load: ' + (data.error || 'unknown'));
        if (!data.ok) stat.style.color = 'var(--danger)';
      } catch (err) {
        stat.textContent = 'Load failed: ' + err;
        stat.style.color = 'var(--danger)';
      } finally {
        btn.disabled = false;
      }
    });
  });
})();
