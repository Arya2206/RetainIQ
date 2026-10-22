/* AI HR Assistant chat widget — renders answers with textContent only (XSS-safe). */
(function(){
  function bubble(log, cls){ const d = document.createElement('div'); d.className = 'chat-bubble ' + cls; log.appendChild(d); return d; }
  function init(root){
    const log = root.querySelector('.chat-log'), form = root.querySelector('form'), input = root.querySelector('input[name=q]');
    const chips = root.querySelector('.chat-chips');
    async function ask(q){
      bubble(log, 'chat-user').textContent = q; input.value = '';
      const b = bubble(log, 'chat-bot'); b.textContent = 'Thinking…'; log.scrollTop = log.scrollHeight;
      try {
        const r = await api('/api/v1/assistant', {json:{question:q}});
        b.textContent = r.answer;
        if (r.items && r.items.length){
          const ul = document.createElement('div'); ul.className = 'mt-2';
          r.items.forEach(it => { const row = document.createElement('div'); row.className = 'small border-top py-1';
            const a = document.createElement('a'); a.href = it.url; a.textContent = it.name; a.className = 'fw-semibold';
            const t = document.createElement('span'); t.className = 'text-muted'; t.textContent = ' · ' + it.dept + ' · ' + it.role + ' · ' + it.score + '/100 (' + it.level + ')';
            row.appendChild(a); row.appendChild(t); ul.appendChild(row); });
          b.appendChild(ul);
        }
        (r.suggestions || []).forEach(s => { const c = document.createElement('span'); c.className = 'chip'; c.textContent = s; c.onclick = () => ask(s); b.appendChild(document.createElement('br')); b.appendChild(c); });
      } catch(e){ b.textContent = 'Sorry — I could not answer that right now.'; }
      log.scrollTop = log.scrollHeight;
    }
    form.addEventListener('submit', e => { e.preventDefault(); const q = input.value.trim(); if (q) ask(q); });
    root.querySelectorAll('.chip[data-q]').forEach(c => c.onclick = () => ask(c.dataset.q));
  }
  document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('.assistant-root').forEach(init));
})();
