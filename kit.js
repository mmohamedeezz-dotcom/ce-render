/* CE motion kit v1 — builds captions from data-vo, assigns entrances, applies timings.
   Renderer calls window.ceApply(T) with real voice timings; in a plain browser it runs on estimates. */
(function () {
  var WORD = 0.36, GAP = 0.18, TAIL = 1.2, OV = 0.45;
  var UNITS = '.k-fit > *:not(.k-list):not(.k-chat):not(.k-bars):not(.k-vs), .k-list > li, .k-chat > p, .k-bars > i, .k-vs > *, .k-card > .k-row';
  function scenes() { return [].slice.call(document.querySelectorAll('.scene')); }
  function toks(s) { return (s.dataset.vo || '').trim().split(/\s+/).filter(Boolean); }

  function build() {
    var root = document.documentElement;
    if (root.dataset.ceBuilt) return; root.dataset.ceBuilt = '1';
    var S = scenes(), brand = document.body.dataset.brand || '', TR = ['up', 'circle', 'side'];
    S.forEach(function (s, i) {
      if (!/\bt-(light|brand|dark)\b/.test(s.className)) s.classList.add(i % 2 ? 't-brand' : 't-light');
      if (i && !s.dataset.tr) s.dataset.tr = TR[(i - 1) % 3];
      if (brand && !s.querySelector('.k-logo')) { var l = document.createElement('div'); l.className = 'k-logo'; l.textContent = brand; s.insertBefore(l, s.firstChild); }
      var w = toks(s);
      // caption: every spoken word on screen, in short chunks, each word landing on its own time
      if (w.length && s.dataset.cap !== 'off' && !s.querySelector('.k-cap')) {
        var cap = document.createElement('div'); cap.className = 'k-cap';
        var hook = s.classList.contains('hook'), maxW = hook ? 3 : 4, maxC = hook ? 13 : 20, ch = null, n = 0, c = 0;
        w.forEach(function (t, k) {
          var clean = t.replace(/\*/g, '');
          if (!ch || n >= maxW || c + clean.length > maxC) { ch = document.createElement('div'); ch.className = 'k-chunk'; cap.appendChild(ch); n = 0; c = 0; }
          var sp = document.createElement('span'); sp.className = 'k-w' + (t.indexOf('*') >= 0 ? ' hl' : ''); sp.textContent = clean; sp.dataset.w = k; ch.appendChild(sp);
          n++; c += clean.length + 1;
          if (/[،.؟!…:,?]$/.test(clean)) n = 99;
        });
        var cs = cap.children, L = cs.length;
        if (L > 1 && cs[L - 1].children.length === 1 && cs[L - 2].children.length > 2) cs[L - 1].insertBefore(cs[L - 2].lastChild, cs[L - 1].firstChild);
        s.appendChild(cap);
      }
      [].forEach.call(s.querySelectorAll('.k-stage'), function (st) { if (st.querySelector(':scope > .k-fit')) return; var f = document.createElement('div'); f.className = 'k-fit'; while (st.firstChild) f.appendChild(st.firstChild); st.appendChild(f); });
      // entrances: spread the visual's parts across the spoken line so something new lands all the time
      var u = [].slice.call(s.querySelectorAll(UNITS)).filter(function (e) { return !e.hasAttribute('data-at'); });
      var W = Math.max(1, w.length);
      u.forEach(function (e, j) { e.setAttribute('data-at', Math.min(W - 1, Math.floor(j * W / Math.max(1, u.length)))); });
    });
  }

  function apply(T) {
    var S = scenes();
    S.forEach(function (s, i) {
      var m = T[i]; if (!m) return; var last = i === S.length - 1, dv = m.d + (last ? 0 : OV);
      s.style.setProperty('--s', m.s + 's'); s.style.setProperty('--d', m.d + 's'); s.style.setProperty('--dv', dv + 's');
      if (last) s.classList.add('ce-last');
      var at = function (k) { k = Math.max(0, Math.min(m.w.length - 1, parseInt(k, 10) || 0)); return m.w[k] || 0; };
      [].forEach.call(s.querySelectorAll('[data-at]'), function (e) { e.style.setProperty('--at', at(e.dataset.at) + 's'); });
      var ch = [].slice.call(s.querySelectorAll('.k-chunk'));
      ch.forEach(function (c, j) {
        [].forEach.call(c.children, function (sp) { sp.style.setProperty('--at', at(sp.dataset.w) + 's'); });
        var cs = at(c.firstChild.dataset.w), end = j < ch.length - 1 ? at(ch[j + 1].firstChild.dataset.w) : dv;
        c.style.setProperty('--cs', cs + 's'); c.style.setProperty('--cd', Math.max(0.2, end - cs) + 's');
        if (last && j === ch.length - 1) c.classList.add('ce-last');
      });
    });
  }

  function estimate() {
    var t = 0, S = scenes();
    return S.map(function (s, i) {
      var n = Math.max(1, toks(s).length), d = n * WORD + 0.35 + GAP + (i === S.length - 1 ? TAIL : 0), o = { s: t, d: d, w: [] };
      for (var k = 0; k < n; k++) o.w.push(k * WORD); t += d; return o;
    });
  }

  // keep every visual inside its zone, and never show a broken icon name as text
  function fit() {
    [].forEach.call(document.querySelectorAll('.k-ico'), function (e) { if (e.scrollWidth > e.clientWidth + 4) e.textContent = 'star'; });
    [].forEach.call(document.querySelectorAll('.k-stage'), function (st) {
      var f = st.querySelector(':scope > .k-fit'); if (!f) return;
      var wid = 0; [].forEach.call(f.children, function (c) { wid = Math.max(wid, c.offsetWidth); });
      var h = Math.max(1, f.offsetHeight), H = st.clientHeight, z = Math.min(1, H / h, 1000 / Math.max(1, wid));
      if (z > 0.995 && h < 0.6 * H) z = Math.min(1.3, 1000 / Math.max(1, wid), 0.78 * H / h);
      if (Math.abs(z - 1) > 0.005) f.style.zoom = z;
    });
    [].forEach.call(document.querySelectorAll('.k-chunk'), function (c) {
      var p = c.parentNode, wide = 0; [].forEach.call(c.children, function (w) { wide = Math.max(wide, w.offsetWidth); });
      var z = Math.min(1, p.clientWidth / Math.max(1, wide), p.clientHeight / Math.max(1, c.scrollHeight));
      if (z < 0.995) c.style.fontSize = (z * 100) + '%';
    });
  }

  window.ceApply = function (T) { build(); apply(T || estimate()); };
  window.ceFit = fit;
  function boot() { if (window.CE_RENDER) return; window.ceApply(); (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(fit); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
