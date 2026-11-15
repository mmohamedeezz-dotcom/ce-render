/* CE motion kit v1 — builds captions from data-vo, assigns entrances, applies timings.
   Renderer calls window.ceApply(T) with real voice timings; in a plain browser it runs on estimates. */
(function () {
  var WORD = 0.36, GAP = 0.18, TAIL = 1.2, OV = 0.45;
  var UNITS = '.k-fit > *:not(.k-list):not(.k-chat):not(.k-bars):not(.k-vs), .k-list > li, .k-chat > p, .k-bars > i, .k-vs > *, .k-card > .k-row';
  function scenes() { return [].slice.call(document.querySelectorAll('.scene')); }
  function toks(s) { return (s.dataset.vo || '').trim().split(/\s+/).filter(Boolean); }

  function lum(c) {
    c = (c || '').trim().replace('#', ''); if (c.length === 3) c = c.replace(/./g, '$&$&');
    if (!/^[0-9a-f]{6}$/i.test(c)) return null; var n = parseInt(c, 16), k = [0.2126, 0.7152, 0.0722];
    return [n >> 16, n >> 8 & 255, n & 255].reduce(function (a, v, i) { v /= 255; return a + k[i] * (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)); }, 0);
  }
  function cr(a, b) { return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); }

  // caption lines: 2-3 words each, sizes balanced, never a lonely word at the end
  function split(w, hook) {
    var dr = isDraw(), maxW = dr ? 4 : hook ? 2 : 3, maxC = dr ? 24 : hook ? 12 : 17, n = w.length, out = [];
    var len = function (t) { return t.replace(/[*\u064B-\u0652]/g, '').length; };
    for (var k = Math.ceil(n / maxW); k <= n; k++) {
      out = []; var base = Math.floor(n / k), extra = n % k, p = 0, ok = true;
      for (var q = 0; q < k; q++) {
        var sz = base + (q < extra ? 1 : 0), part = [], c = -1;
        for (var x = p; x < p + sz; x++) { part.push({ t: w[x], k: x }); c += len(w[x]) + 1; }
        p += sz; out.push(part); if (c > maxC && sz > 1) ok = false;
      }
      if (ok) break;
    }
    return out;
  }
  function seed(str) { var h = 7; for (var i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 999983; return h; }
  function imgs() { return [].slice.call(document.querySelectorAll('.scene[data-img], .k-photo[data-img]')); }
  function isDraw() { return document.body.dataset.style === 'draw'; }
  var DRAW_STYLE = 'Vintage 1960s comic book illustration, bold black ink outlines, flat warm colours in orange, teal and brown, aged paper texture, full bleed with no border and no frame. ';
  function imgSpec(e) {
    var draw = isDraw(), sq = !e.classList.contains('scene');
    var p = draw ? DRAW_STYLE + e.dataset.img + '. No text, no letters, no numbers, no dates, no signs, no labels, no speech bubbles.'
                 : e.dataset.img + ', photorealistic photo, natural light, 50mm lens, shallow depth of field, no text, no letters, no watermark';
    var w = 1080, h = sq ? 1080 : 2048, sd = seed(e.dataset.img);
    return { prompt: p, w: w, h: h, seed: sd, move: (draw && e.dataset.move) || '', url: 'https://image.pollinations.ai/prompt/' + encodeURIComponent(p) + '?width=' + w + '&height=' + h + '&model=flux&nologo=true&seed=' + sd };
  }
  function setImg(i, uri, fg, clip) {
    var e = imgs()[i]; if (!e) return; var sc = e.classList.contains('scene'), t = sc ? e.querySelector('.k-bg') : e;
    if (uri && i < (isDraw() ? 12 : 4)) {
      if (fg && sc) {  // two layers: the far one and the near one move against each other
        t.innerHTML = ''; ['k-far', 'k-near'].forEach(function (c, k) { var d = document.createElement('div'); d.className = c; d.style.backgroundImage = 'url("' + (k ? fg : uri) + '")'; t.appendChild(d); });
        t.classList.add('k-deep');
      } else t.style.backgroundImage = 'url("' + uri + '")';
      if (clip && sc) { var v = document.createElement('div'); v.className = 'k-vid'; v.style.backgroundImage = 'url("' + clip + '")'; t.appendChild(v); t.classList.add('k-clip'); }
      e.dataset.imgOk = '1'; return;
    }
    if (sc) { if (t) t.remove(); e.classList.remove('t-photo'); e.classList.add('t-dark'); } else e.style.display = 'none';
    e.dataset.imgOk = '0';
  }

  function build() {
    var root = document.documentElement;
    if (root.dataset.ceBuilt) return; root.dataset.ceBuilt = '1';
    var S = scenes(), brand = document.body.dataset.brand || '', TR = ['up', 'circle', 'side'];
    if (isDraw()) document.body.classList.add('draw');
    var BT = brand.split(/\s+/); // brand words keep their own spelling on screen
    // readable colours whatever palette the brand has
    var cs = getComputedStyle(document.body), bs = document.body.style;
    var La = lum(cs.getPropertyValue('--accent')), Lb = lum(cs.getPropertyValue('--brand')), Li = lum(cs.getPropertyValue('--ink')), Lp = lum(cs.getPropertyValue('--paper'));
    if (La != null && Li != null) bs.setProperty('--on-accent', cr(La, 1) > cr(La, Li) ? '#fff' : 'var(--ink)');
    if (Lb != null && Li != null) bs.setProperty('--on-brand', cr(Lb, 1) >= 3 || cr(Lb, 1) > cr(Lb, Li) ? '#fff' : 'var(--ink)');
    if (La != null) bs.setProperty('--hl-dark', La >= 0.12 ? 'var(--accent)' : (Lb != null && Lb >= 0.12 ? 'var(--brand)' : '#FFD166'));
    if (La != null && Lb != null && cr(La, Lb) < 2.4) document.body.classList.add('lowc');
    if (Lb != null && Lp != null && cr(Lb, Lp) < 2.6) document.body.classList.add('lowb');
    S.forEach(function (s, i) {
      if (s.dataset.img && !s.querySelector('.k-bg')) { var bg = document.createElement('div'); bg.className = 'k-bg'; s.insertBefore(bg, s.firstChild); s.classList.remove('t-light', 't-brand', 't-dark'); s.classList.add('t-photo'); }
      if (!/\bt-(light|brand|dark|photo)\b/.test(s.className)) s.classList.add(i % 2 ? 't-brand' : 't-light');
      if (i && !s.dataset.tr) s.dataset.tr = isDraw() ? 'dip' : TR[(i - 1) % 3];
      if (brand && !s.querySelector('.k-logo')) { var l = document.createElement('div'); l.className = 'k-logo'; l.textContent = brand; s.insertBefore(l, s.firstChild); }
      var w = toks(s);
      // caption: every spoken word on screen, in short chunks, each word landing on its own time
      if (w.length && s.dataset.cap !== 'off' && !s.querySelector('.k-cap')) {
        var cap = document.createElement('div'); cap.className = 'k-cap';
        var hook = s.classList.contains('hook');
        split(w, hook).forEach(function (part) {
          var ch = document.createElement('div'); ch.className = 'k-chunk'; cap.appendChild(ch);
          part.forEach(function (o) {
            var clean = o.t.replace(/\*/g, ''), sp = document.createElement('span'); sp.className = 'k-w' + (o.t.indexOf('*') >= 0 ? ' hl' : '');
            var shown = clean.replace(/[،,.]+$/, ''); sp.textContent = BT.indexOf(shown) >= 0 ? shown : shown.replace(/[\u064B-\u0650\u0652\u0670]/g, ''); sp.dataset.w = o.k; ch.appendChild(sp);
          });
        });
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
      if (i === 0) dv += 0.06; // the very first frame must already show the picture
      s.style.setProperty('--s', (i === 0 ? -0.06 : m.s) + 's'); s.style.setProperty('--d', m.d + 's'); s.style.setProperty('--dv', dv + 's');
      if (last) s.classList.add('ce-last');
      var at = function (k) { k = Math.max(0, Math.min(m.w.length - 1, parseInt(k, 10) || 0)); var v = m.w[k] || 0; return i === 0 && v < 0.25 ? -0.22 : v; };
      [].forEach.call(s.querySelectorAll('[data-at]'), function (e) { e.style.setProperty('--at', at(e.dataset.at) + 's'); });
      var ch = [].slice.call(s.querySelectorAll('.k-chunk'));
      ch.forEach(function (c, j) {
        [].forEach.call(c.children, function (sp) { sp.style.setProperty('--at', at(sp.dataset.w) + 's'); });
        var cs = at(c.firstChild.dataset.w), end = j < ch.length - 1 ? at(ch[j + 1].firstChild.dataset.w) : dv;
        c.style.setProperty('--cs', cs + 's'); c.style.setProperty('--cd', Math.max(0.2, end - cs) + 's');
        if (last && j === ch.length - 1) c.classList.add('ce-last');
      });
      s.__ce = { dv: dv, cuts: ch.slice(1).map(function (c) { return Math.max(0.05, at(c.firstChild.dataset.w)); }) };
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
      var p = c.parentNode, fs = parseFloat(getComputedStyle(c).fontSize), tot = -0.26 * fs;
      [].forEach.call(c.children, function (w) { tot += w.offsetWidth + 0.26 * fs; });
      var z = Math.min(1, p.clientWidth / Math.max(1, tot));
      if (z < 0.995) c.style.fontSize = (z * 100) + '%';
    });
    punch();
  }

  function punch() {
    var css = '';
    scenes().forEach(function (s, i) {
      var st = s.querySelector('.k-stage'), f = st && st.querySelector(':scope > .k-fit'), m = s.__ce; if (!f || !m) return;
      var w = f.offsetWidth * (parseFloat(f.style.zoom) || 1), max = Math.max(1, Math.min(1.12, 1010 / Math.max(1, w)));
      var n = m.cuts.length, drift = Math.min(0.03, (max - 1) * 0.4), step = n ? Math.min(0.035, (max - 1 - drift) / n) : 0, dv = m.dv;
      var E = 'animation-timing-function:cubic-bezier(.16,1,.3,1);', L = 'animation-timing-function:linear;';
      var kf = ['0%{transform:scale(1);' + L + '}'], sc = 1, pt = 0;
      m.cuts.forEach(function (c) {
        sc += drift * (c - pt) / dv; kf.push((c / dv * 100).toFixed(2) + '%{transform:scale(' + sc.toFixed(4) + ');' + E + '}');
        sc += step; kf.push((Math.min(dv, c + 0.45) / dv * 100).toFixed(2) + '%{transform:scale(' + sc.toFixed(4) + ');' + L + '}'); pt = Math.min(dv, c + 0.45);
      });
      sc += drift * (dv - pt) / dv; kf.push('100%{transform:scale(' + Math.min(max, sc).toFixed(4) + ')}');
      css += '@keyframes kd' + i + '{' + kf.join('') + '}'; st.style.animationName = 'kd' + i;
    });
    var tag = document.getElementById('ce-kf') || document.head.appendChild(Object.assign(document.createElement('style'), { id: 'ce-kf' }));
    tag.textContent = css;
  }

  // review of the finished layout: every problem a viewer could see, counted
  function qa() {
    var out = { off_frame: [], overflow: [], overlap: [], scenes: scenes().length };
    document.getAnimations().forEach(function (a) { try { a.finish(); } catch (e) {} });
    scenes().forEach(function (s, i) {
      var f = s.querySelector('.k-fit'), cap = s.querySelector('.k-cap');
      [].forEach.call(s.querySelectorAll('.k-fit > *:not(.k-stat):not(.k-price), .k-stat > *, .k-price > *, .k-w'), function (e) {
        var r = e.getBoundingClientRect(); if (!r.width) return;
        if (r.left < 4 || r.right > 1076 || r.top < 0 || r.bottom > 1920) out.off_frame.push((i + 1) + ': ' + (e.textContent || e.className).trim().slice(0, 24));
      });
      [].forEach.call(s.querySelectorAll('.k-card b, .k-card small, .k-card em, .k-list li, .k-chat p, .k-cta, .k-tag, .k-stat small'), function (e) {
        if (e.scrollWidth > e.clientWidth + 3) out.overflow.push((i + 1) + ': ' + e.textContent.trim().slice(0, 24));
      });
      if (f && cap) { var a = f.getBoundingClientRect(), b = cap.getBoundingClientRect(); if (a.bottom > b.top + 6 && !s.classList.contains('end')) out.overlap.push(i + 1); }
    });
    return out;
  }

  window.ceApply = function (T) { build(); apply(T || estimate()); };
  window.ceFit = fit; window.ceQA = qa;
  window.ceImages = function () { return imgs().map(imgSpec); }; window.ceSetImage = setImg;
  function boot() { if (window.CE_RENDER) return; window.ceApply(); imgs().forEach(function (e, i) { setImg(i, imgSpec(e).url); }); (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(fit); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
