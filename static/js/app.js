/* RetainIQ shared front-end helpers (no external dependencies) */
(function(){
  const meta = document.querySelector('meta[name="csrf-token"]');
  window.CSRF = meta ? meta.content : '';
  window.api = async function(url, opts){
    opts = opts || {};
    opts.headers = Object.assign({'X-CSRF-Token': window.CSRF, 'X-Requested-With': 'fetch'}, opts.headers || {});
    if (opts.json){ opts.method = opts.method || 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.json); }
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error('Request failed (' + r.status + ')');
    return r.json();
  };
  // respect reduced-motion preference (also keeps automated screenshots deterministic)
  if (window.Chart && (navigator.webdriver || (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches))) { Chart.defaults.animation = false; }
  window.riskColor = s => s >= 60 ? '#e5484d' : (s >= 30 ? '#f5a524' : '#30a46c');
  window.levelOf = s => s >= 60 ? 'High' : (s >= 30 ? 'Medium' : 'Low');
  window.esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // theme toggle
  const root = document.documentElement;
  function applyTheme(t){ root.setAttribute('data-bs-theme', t); try{localStorage.setItem('retainiq-theme', t);}catch(e){}
    const i = document.getElementById('themeIcon'); if (i) i.className = 'bi ' + (t === 'dark' ? 'bi-sun' : 'bi-moon-stars');
    if (window.Chart){ Chart.defaults.color = t === 'dark' ? '#aab4d0' : '#5b6684'; Chart.defaults.borderColor = t === 'dark' ? '#26324f' : '#e4e8f2'; } }
  window.toggleTheme = () => { applyTheme(root.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark'); };
  document.addEventListener('DOMContentLoaded', () => applyTheme(root.getAttribute('data-bs-theme') || 'light'));

  // half-circle risk gauge (0-100)
  window.drawGauge = function(id, score){
    const el = document.getElementById(id); if (!el) return null;
    const color = riskColor(score);
    return new Chart(el, {type:'doughnut',
      data:{datasets:[{data:[score, 100 - score], backgroundColor:[color, root.getAttribute('data-bs-theme') === 'dark' ? '#26324f' : '#e9edf6'], borderWidth:0}]},
      options:{rotation:270, circumference:180, cutout:'76%', responsive:true, maintainAspectRatio:false,
        plugins:{legend:{display:false}, tooltip:{enabled:false}}},
      plugins:[{id:'gtext', afterDraw(c){ const a = c.getDatasetMeta(0).data[0]; if(!a) return; const x = c.ctx; x.save();
        x.textAlign = 'center'; x.fillStyle = color; x.font = '800 34px Inter, Segoe UI, sans-serif'; x.fillText(score, a.x, a.y - 14);
        x.fillStyle = getComputedStyle(document.body).color; x.globalAlpha = .6; x.font = '600 11px Inter, Segoe UI, sans-serif';
        x.fillText('RISK SCORE / 100', a.x, a.y + 4); x.restore(); }}]});
  };
  window.markAllRead = () => fetch('/notifications/read-all', {method:'POST', headers:{'X-CSRF-Token':window.CSRF,'X-Requested-With':'fetch'}}).then(() => location.reload());
})();
