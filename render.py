import urllib.error
#!/usr/bin/env python3
"""CE renderer v2 (voice-first). HTML scenes -> MP4, timed to the real voice-over.
usage: render.py spec.json outdir
spec: {code, kind: reel|post, html, voice_id, fps}
HTML contract (reel): <section class="scene" data-vo="spoken line" [data-sfx="english sound prompt"]> ... </section>
  inside a scene, any element with data-at="N" appears when spoken word N (0-based) of that scene starts.
The renderer measures each line's real speech, then sets on every scene --s (start, s) and --d (duration, s),
and on every [data-at] element --at (offset inside the scene, s). Base CSS below does the rest.
"""
import base64, hashlib, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from playwright.sync_api import sync_playwright

spec = json.load(open(sys.argv[1], encoding='utf-8'))
out = sys.argv[2]; os.makedirs(out, exist_ok=True)
code = ''.join(c for c in str(spec.get('code') or 'asset') if c.isalnum() or c in '-_') or 'asset'
reel = spec.get('kind') == 'reel'
W, H = 1080, (1920 if reel else 1350)
if not reel and int(spec.get('w') or 0) >= 200 and int(spec.get('h') or 0) >= 200:
    W, H = min(int(spec['w']), 4000), min(int(spec['h']), 4000)  # designs come in every platform size
fps = 60 if reel else int(spec.get('fps') or 30)  # 60 for smooth motion
KEY = os.environ.get('ELEVENLABS_API_KEY', '')
voice = spec.get('voice_id') or os.environ.get('ELEVENLABS_VOICE_ID', '')
GAP, TAIL, WORD = 0.18, 1.2, 0.36

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_CSS = open(os.path.join(HERE, 'kit.css'), encoding='utf-8').read()
KIT_JS = open(os.path.join(HERE, 'kit.js'), encoding='utf-8').read()

def ff(*a):
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', *a], check=True)

def post(path, body):
    if os.environ.get('CE_MOCK'):
        import ce_mock; return ce_mock.post(path, body)
    req = urllib.request.Request('https://api.elevenlabs.io' + path, data=json.dumps(body).encode(),
                                 headers={'xi-api-key': KEY, 'Content-Type': 'application/json'})
    try:
        return urllib.request.urlopen(req, timeout=180).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError('HTTP %s: %s' % (e.code, e.read().decode('utf8','replace')[:300]))

CACHE = os.environ.get('CE_CACHE', 'cache'); os.makedirs(CACHE, exist_ok=True)
import ce_img
import ce_i2v
ASSETS = {'images_wanted': 0, 'images_ok': 0, 'music': ''}

def brand_music(brand, prompt, dest):
    """one instrumental bed per brand, generated once and reused on every reel of that brand."""
    f = os.path.join(CACHE, 'music_' + hashlib.sha1((brand + '|' + str(globals().get('STYLE', ''))).encode()).hexdigest()[:16] + '.mp3')
    if not (os.path.exists(f) and os.path.getsize(f) > 50000):
        if not KEY: return None
        p = (prompt or 'warm modern instrumental background music for a short social ad, light drums, soft bass, positive, steady groove') + '. Instrumental only, no vocals, no speech.'
        for body in ({'prompt': p, 'music_length_ms': 45000, 'force_instrumental': True}, {'prompt': p, 'music_length_ms': 45000}):
            try:
                data = post('/v1/music', body)
                if len(data) > 50000: open(f, 'wb').write(data); print('music: generated new bed for this brand'); break
            except Exception as e:
                print('music failed:', str(e)[:200])
        else:
            return None
    else:
        print('music: reused the saved bed for this brand')
    return f

def speak(text, dest):
    """returns (duration, [word start times]) using the real voice; None if unavailable."""
    text = text.replace('*', '')
    if not (KEY and voice and text.strip()):
        return None
    try:
        r = json.loads(post(f'/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128',
                            {'text': text, 'model_id': spec.get('model_id') or 'eleven_multilingual_v2'}))
        open(dest, 'wb').write(base64.b64decode(r['audio_base64']))
        al = r.get('alignment') or {}
        ch, st, en = al.get('characters') or [], al.get('character_start_times_seconds') or [], al.get('character_end_times_seconds') or []
        words, prev = [], ' '
        for c, t in zip(ch, st):
            if not c.isspace() and prev.isspace():
                words.append(float(t))
            prev = c
        dur = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', dest]).decode().strip())
        return max(dur, float(en[-1]) if en else 0), words
    except Exception as e:
        print('VO failed:', e); return None

def adur(f):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', f]).decode().strip())

def silences(f, db, d):
    log = subprocess.run(['ffmpeg', '-hide_banner', '-i', f, '-af', f'silencedetect=noise={db}dB:d={d}', '-f', 'null', '-'], capture_output=True, text=True).stderr
    ss = [max(0.0, float(x)) for x in re.findall(r'silence_start: (-?[0-9.]+)', log)]; ee = [float(x) for x in re.findall(r'silence_end: ([0-9.]+)', log)]
    return list(zip(ss, ee))

def sync_err(f, starts):
    """how far each scene's first word is from a real speech onset in the audio (seconds): (max, mean)."""
    if not starts: return 0.0, 0.0
    on = [0.0] + [e for _, e in silences(f, -32, 0.12)]
    errs = [min(abs(t - o) for o in on) for t in starts]
    return max(errs), sum(errs) / len(errs)

def tighten(raw, dest, words):
    """cap every pause at 0.3s; returns moved word times, or None if the cut did not come out exact."""
    cuts = [(a + (0.05 if a <= 0.01 else 0.3), b - 0.03) for a, b in silences(raw, -38, 0.36)]
    cuts = [(a, b) for a, b in cuts if b - a > 0.05]
    if not cuts: return None
    keep, p = [], 0.0
    for a, b in cuts: keep.append((p, a)); p = b
    keep.append((p, None))
    flt = ';'.join(f'[0:a]atrim=start={x:.3f}' + (f':end={y:.3f}' if y is not None else '') + f',asetpts=PTS-STARTPTS[k{i}]' for i, (x, y) in enumerate(keep))
    flt += ';' + ''.join(f'[k{i}]' for i in range(len(keep))) + f'concat=n={len(keep)}:v=0:a=1[o]'
    ff('-i', raw, '-filter_complex', flt, '-map', '[o]', '-ar', '44100', dest)
    want = adur(raw) - sum(b - a for a, b in cuts)
    if abs(adur(dest) - want) > 0.12:
        print(f'pause cut not exact ({adur(dest):.2f}s vs {want:.2f}s), dropped'); return None
    def mv(t):
        off = 0.0
        for a, b in cuts:
            if t >= b: off += b - a
            elif t > a: return a - off
        return t - off
    return [mv(t) for t in words]

VOICE = {'mode': 'none'}

def speak_all(texts, dest):
    """one take for the whole script; every timing candidate is checked against the audio itself and the best one wins."""
    if not (KEY and voice): return None
    parts = [t.replace('*', '').strip() for t in texts]
    if not all(parts): return None
    parts = [p if re.search(r'[.!?؟…]$', p) else p + '.' for p in parts]
    full, counts = ' '.join(parts), [len(p.split()) for p in parts]
    offs = [sum(counts[:i]) for i in range(len(counts))]
    sp = 0.98 if globals().get('STYLE') == 'draw' else 1.08  # a story is told slower than an ad
    v3 = {'stability': 0.5, 'similarity_boost': 0.8, 'speed': sp}
    v2 = {'stability': 0.45, 'similarity_boost': 0.8, 'style': 0.3, 'use_speaker_boost': True, 'speed': sp}
    tries = [('eleven_v3', v3, 'ar'), ('eleven_v3', None, 'ar'), ('eleven_multilingual_v2', v2, None), ('eleven_multilingual_v2', None, None)]
    if spec.get('model_id'): tries.insert(0, (spec['model_id'], None, None))
    raw = dest + '.raw.mp3'
    for model, vs, lang in tries:
        body = {'text': full, 'model_id': model}
        if vs: body['voice_settings'] = vs
        if lang: body['language_code'] = lang
        try:
            r = json.loads(post(f'/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128', body))
            al = r.get('alignment') or r.get('normalized_alignment') or {}
            ch, st, en = al.get('characters') or [], al.get('character_start_times_seconds') or [], al.get('character_end_times_seconds') or []
            words, prev = [], ' '
            for c, t in zip(ch, st):
                if not c.isspace() and prev.isspace(): words.append(float(t))
                prev = c
            if len(words) != sum(counts):
                print(f'VO {model}: word count mismatch {len(words)} vs {sum(counts)}'); continue
            open(raw, 'wb').write(base64.b64decode(r['audio_base64']))
        except Exception as e:
            print(f'VO {model} failed:', e); continue
        first = lambda w: [w[o] for o in offs[1:]]
        cands = [('as delivered', raw, words)]
        sp = silences(raw, -38, 0.2); speech_end = sp[-1][0] if sp and sp[-1][1] >= adur(raw) - 0.05 else adur(raw)
        if en and en[-1] > 0.5 and abs(speech_end / en[-1] - 1) > 0.03:
            k = speech_end / en[-1]; cands.append((f'rescaled x{k:.3f}', raw, [t * k for t in words]))
        best = min(cands, key=lambda c: sync_err(c[1], first(c[2]))[1])
        tw = tighten(best[1], dest + '.cut.wav', best[2])
        if tw: cands.append((best[0] + ' + pauses tightened', dest + '.cut.wav', tw))
        scored = [(sync_err(c[1], first(c[2])), c) for c in cands]
        for (mx, mean), c in scored: print(f'  sync check [{c[0]}]: max {mx:.2f}s mean {mean:.2f}s')
        ok = [x for x in scored if x[0][0] <= 0.25 and x[0][1] <= 0.12]
        if not ok:
            print(f'VO {model}: no timing passed the sync check'); continue
        (mx, mean), (name, f, w) = ok[-1] if ok[-1][1][1].endswith('.cut.wav') else min(ok, key=lambda x: x[0][1])
        ff('-i', f, '-ar', '44100', dest)
        VOICE.update(mode='one take', model=model, timing=name, sync_max=round(mx, 3), sync_mean=round(mean, 3))
        print(f'VO one take: model={model} timing={name} words={len(w)} dur={adur(dest):.2f}s')
        return adur(dest), w, counts
    return None

def estimate(text):
    n = max(1, len(text.replace('*', '').split()))
    return n * WORD + 0.35, [i * WORD for i in range(n)]

html = spec['html']
# the kit always comes from this repo: drop any CDN copy the page links to, then inline ours
html = re.sub(r'<link[^>]+kit\.css[^>]*>', '', html)
html = re.sub(r'<script[^>]+kit\.js[^>]*>\s*</script>', '', html)
inject = '<style id="ce-kit-css">' + KIT_CSS + '</style><script id="ce-kit-flag">window.CE_RENDER=1</script><script id="ce-kit-js">' + KIT_JS + '</script>'
html = html.replace('<head>', '<head>' + inject, 1) if '<head>' in html else inject + html

def local_fonts(pg):
    """test only: serve fonts from a local folder when the machine cannot reach Google Fonts."""
    d = os.environ.get('CE_FONTS')
    if not d: return
    css = open(os.path.join(d, 'fonts.css'), encoding='utf-8').read()
    pg.route('**/fonts.googleapis.com/**', lambda r: r.fulfill(status=200, content_type='text/css', body=css if 'Tajawal' in r.request.url else ''))
    pg.route('**/ce-local-fonts/**', lambda r: r.fulfill(status=200, content_type='font/woff2', body=open(os.path.join(d, r.request.url.rsplit('/', 1)[1]), 'rb').read()))

with sync_playwright() as p:
    kw = {'executable_path': os.environ['CHROME']} if os.environ.get('CHROME') else {}
    b = p.chromium.launch(args=['--no-sandbox', '--font-render-hinting=none'], **kw)
    pg = b.new_page(viewport={'width': W, 'height': H}, device_scale_factor=1)
    local_fonts(pg)
    pg.set_content(html, wait_until='networkidle'); pg.evaluate('document.fonts.ready')
    if not reel:
        # designs: real photos are generated for every <img data-img="english prompt"> that has no http src yet
        try:
            shots = pg.evaluate("() => [].slice.call(document.querySelectorAll('img[data-img]')).map((e, i) => { const r = e.getBoundingClientRect(); return { i: i, prompt: e.getAttribute('data-img') || '', w: Math.round(r.width), h: Math.round(r.height), has: /^https?:/.test(e.getAttribute('src') || '') }; })")
        except Exception as e:
            shots = []; print('img scan failed:', str(e)[:160])
        import shutil
        for u in shots[:4]:
            if u['has'] or not u['prompt'].strip(): continue
            gw, gh = (768, 1344) if u['h'] > u['w'] * 1.15 else ((1344, 768) if u['w'] > u['h'] * 1.15 else (1024, 1024))
            f = None
            try: f = ce_img.fetch_image(u['prompt'], gw, gh, 1 + u['i'])
            except Exception as e: print('image error:', str(e)[:160])
            if not f: print('image failed', u['i']); continue
            shutil.copy(f, f"{out}/{code}_img{u['i']}.jpg")
            pg.evaluate('([i, d]) => new Promise(res => { const e = document.querySelectorAll("img[data-img]")[i]; e.onload = () => res(1); e.onerror = () => res(0); e.src = d; })', [u['i'], ce_img.data_uri(f)])
        pg.screenshot(path=f'{out}/{code}.png'); b.close(); print('png done'); sys.exit(0)

    STYLE = pg.evaluate("document.body.dataset.style || ''")
    # one real background for the whole reel: the brand's saved one (data-bg-url) or a new one from <body data-bg="english prompt">
    BG = pg.evaluate("document.body.dataset.bg || ''").strip(); BGU = pg.evaluate("document.body.dataset.bgUrl || ''").strip()
    if BG or BGU:
        import shutil
        bgf = None
        if BGU.startswith('https://cdn.jsdelivr.net/gh/'):
            try:
                bgf = os.path.join(ce_img.CACHE, 'bg_' + hashlib.sha1(BGU.encode()).hexdigest()[:16] + '.jpg')
                if not (os.path.exists(bgf) and os.path.getsize(bgf) > 20000): open(bgf, 'wb').write(ce_img._get(BGU))
            except Exception as e: bgf = None; print('brand background download failed:', str(e)[:160])
        if not bgf and BG:
            os.environ['CE_PRO'] = '1'
            try: bgf = ce_img.fetch_image(BG + ', vertical composition, soft focus, calm, plenty of empty space, no people facing camera, no text, no letters, no logos, no watermark', 1080, 1920, 7)
            except Exception as e: bgf = None; print('bg image error:', str(e)[:160])
            os.environ.pop('CE_PRO', None)
            if bgf: shutil.copy(bgf, f'{out}/{code}_bg.jpg')  # published so the brand keeps this one background for every reel
        if bgf:
            pg.evaluate("d => { document.body.style.setProperty('--bgimg', 'url(\"' + d + '\")'); document.body.classList.add('has-bg'); const fx = document.createElement('i'); fx.className = 'k-bgfx'; document.body.insertBefore(fx, document.body.firstChild); document.querySelectorAll('.scene').forEach(s => { s.classList.remove('t-light', 't-brand'); s.classList.add('t-dark'); }); }", ce_img.data_uri(bgf))
            print('reel background: ok')
        else: print('reel background: failed, plain scenes kept')
    scenes = pg.evaluate("[...document.querySelectorAll('.scene')].map(s => ({vo: s.dataset.vo || '', sfx: s.dataset.sfx || ''}))")
    if not scenes:
        print('no .scene sections'); sys.exit(1)
    timing, t, tracks, real = [], 0.0, [], 0
    one = speak_all([sc['vo'] for sc in scenes], f'{out}/_vo.wav')
    if one:
        dur, words, counts = one; real = len(scenes); tracks.append((f'{out}/_vo.wav', 0.0, 1.0, False))
        offs = [sum(counts[:i]) for i in range(len(counts))]
        starts = [0.0] + [max(0.0, words[o] - 0.25) for o in offs[1:]]  # the cut lands just before the word, never after it
        for i, o in enumerate(offs):
            last = i == len(offs) - 1
            d = (dur + TAIL if last else starts[i + 1]) - starts[i]
            timing.append({'s': round(starts[i], 3), 'd': round(d, 3), 'w': [round(max(0.0, x - starts[i]), 3) for x in words[o:o + counts[i]]]})
        t = dur + TAIL
    else:
        for i, sc in enumerate(scenes):
            f = f'{out}/_vo{i}.mp3'
            r = speak(sc['vo'], f)
            if r: real += 1; tracks.append((f, t, 1.0, False))
            if r: VOICE.update(mode='per scene', model=spec.get('model_id') or 'eleven_multilingual_v2', sync_max=0.0, sync_mean=0.0)
            dur, words = r or estimate(sc['vo'])
            d = dur + GAP + (TAIL if i == len(scenes) - 1 else 0)
            timing.append({'s': round(t, 3), 'd': round(d, 3), 'w': [round(x, 3) for x in words]})
            t += d
    for i, sc in enumerate(scenes):
        if sc['sfx'] and KEY:
            g = f'{out}/_sfx{i}.mp3'
            try:
                open(g, 'wb').write(post('/v1/sound-generation', {'text': sc['sfx'], 'duration_seconds': 1.0, 'prompt_influence': 0.5}))
                tracks.append((g, timing[i]['s'], 0.4, True))
            except Exception as e:
                print('SFX failed:', e)
    total = t
    print(f'scenes={len(scenes)} real_vo={real} total={total:.2f}s')

    # real timings into the DOM (captions, entrances, scene windows), fit every visual to its zone, then reload clean
    pg.evaluate('T => window.ceApply(T)', timing)
    urls = pg.evaluate('window.ceImages()')
    IMG_MAX = 12 if STYLE == 'draw' else 4
    ASSETS['images_wanted'] = len(urls)
    for k, u in enumerate(urls):
        f = ce_img.fetch_image(u['prompt'], u['w'], u['h'], u['seed']) if k < IMG_MAX else None
        if f:
            L = ce_img.make_layers(f) if (STYLE == 'draw' and os.environ.get('CE_DEPTH')) else None  # depth warp is off: it distorted the drawings
            if L: ASSETS['deep'] = ASSETS.get('deep', 0) + 1
            V = ce_i2v.clip_sprite(f, u.get('move') or 'The subject makes one small natural movement, like a slow blink or a slight turn of the head.') if STYLE == 'draw' else None
            if V: ASSETS['clips'] = ASSETS.get('clips', 0) + 1
            pg.evaluate('([i, d, g, v]) => window.ceSetImage(i, d, g, v)', [k, ce_img.data_uri(L[0] if L else f), ce_img.data_uri(L[1]) if L else None, ce_img.data_uri(V) if V else None]); ASSETS['images_ok'] += 1
        else:
            pg.evaluate('i => window.ceSetImage(i, null)', k)
    ASSETS['providers'] = dict(ce_img.USED)
    BRAND = pg.evaluate("document.body.dataset.brand || ''"); MUSIC_PROMPT = pg.evaluate("document.body.dataset.music || ''")
    pg.evaluate('document.fonts.ready'); pg.wait_for_timeout(300)
    pg.evaluate("window.ceFit(); ['ce-kit-js','ce-kit-flag'].forEach(i => { const e = document.getElementById(i); if (e) e.remove(); })")
    final_html = pg.evaluate('"<!DOCTYPE html>" + document.documentElement.outerHTML')
    LAYOUT = pg.evaluate('window.ceQA()')
    pg.set_content(final_html, wait_until='networkidle'); pg.evaluate('document.fonts.ready')
    pg.evaluate('document.getAnimations().forEach(a => a.pause())')
    silent = f'{out}/_silent.mp4'
    enc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', str(fps), '-c:v', 'mjpeg', '-i', '-',
                            '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', silent], stdin=subprocess.PIPE)
    for i in range(int(round(total * fps))):
        pg.evaluate('t => document.getAnimations().forEach(a => { a.currentTime = t })', i * 1000.0 / fps)
        enc.stdin.write(pg.screenshot(type='jpeg', quality=95))
    enc.stdin.close(); enc.wait(); b.close()

def mix(sel, dest, norm=''):
    if not sel: return False
    ins, flt = [], []
    for k, (f, st, g, _) in enumerate(sel):
        ins += ['-i', f]; flt.append(f'[{k}:a]aresample=44100,volume={g},adelay={int(st*1000)}:all=1[a{k}]')
    flt.append(''.join(f'[a{k}]' for k in range(len(sel))) + f'amix=inputs={len(sel)}:normalize=0:dropout_transition=0,apad,atrim=0:{total},' + norm + 'alimiter=limit=0.95[m]')
    ff(*ins, '-filter_complex', ';'.join(flt), '-map', '[m]', dest); return True

final = f'{out}/{code}.mp4'
music = brand_music(BRAND or code, MUSIC_PROMPT, f'{out}/_music.mp3') if (reel and real) else None
ASSETS['music'] = 'yes' if music else 'no'

def final_mix(dest):
    """voice on top, effects under it, the brand's music lowest and ducked whenever someone speaks."""
    if not tracks: return False
    ins, flt = [], []
    for k, (f, st, g, _) in enumerate(tracks):
        ins += ['-i', f]; flt.append(f'[{k}:a]aresample=44100,volume={g},adelay={int(st*1000)}:all=1[a{k}]')
    vo = [k for k, x in enumerate(tracks) if not x[3]]; fx = [k for k, x in enumerate(tracks) if x[3]]
    buses = []
    if vo:
        flt.append(''.join(f'[a{k}]' for k in vo) + f'amix=inputs={len(vo)}:normalize=0:dropout_transition=0,apad,atrim=0:{total}' + (',asplit[vb][key]' if music else '[vb]')); buses.append('[vb]')
    if fx:
        flt.append(''.join(f'[a{k}]' for k in fx) + f'amix=inputs={len(fx)}:normalize=0:dropout_transition=0,apad,atrim=0:{total}[fb]'); buses.append('[fb]')
    if music:
        m = len(tracks); ins += ['-stream_loop', '-1', '-i', music]
        flt.append(f'[{m}:a]aresample=44100,atrim=0:{total},volume=0.3,afade=t=in:d=0.4,afade=t=out:st={max(0, total - 1.4):.2f}:d=1.4[mu]')
        if vo: flt.append('[mu][key]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=380[md]'); buses.append('[md]')
        else: buses.append('[mu]')
    flt.append(''.join(buses) + f'amix=inputs={len(buses)}:normalize=0:dropout_transition=0,apad,atrim=0:{total},loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100,alimiter=limit=0.95[m]')
    ff(*ins, '-filter_complex', ';'.join(flt), '-map', '[m]', dest); return True

if final_mix(f'{out}/_mix.wav'):
    ff('-i', silent, '-i', f'{out}/_mix.wav', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', final)
    mix([x for x in tracks if x[3]], f'{out}/{code}_sfx.mp3')
else:
    os.replace(silent, final)

# ---------- review of the finished file: what the viewer will actually get ----------
def qa_report():
    L = []
    def add(ok, t): L.append({'ok': bool(ok), 't': t})
    try:
        px = subprocess.run(['ffmpeg', '-v', 'error', '-i', final, '-vf', r'select=eq(n\,0),scale=32:32,format=gray', '-frames:v', '1', '-f', 'rawvideo', '-'], capture_output=True).stdout
        m = sum(px) / max(1, len(px)); sd = (sum((p - m) ** 2 for p in px) / max(1, len(px))) ** 0.5
        add(sd > 10, 'أول فريم فيه صورة مش فاضي' if sd > 10 else 'أول فريم فاضي')
        fz = subprocess.run(['ffmpeg', '-hide_banner', '-i', final, '-vf', 'freezedetect=n=0.0015:d=1.0', '-an', '-f', 'null', '-'], capture_output=True, text=True).stderr
        mx = max([float(x) for x in re.findall(r'freeze_duration: ([0-9.]+)', fz)] or [0.0])
        add(mx <= 2.0, f'أطول وقفة من غير حركة {mx:.1f} ثانية' + ('' if mx <= 2.0 else ' (أكتر من ثانيتين)'))
        d = adur(final); add(d >= 6, f'المدة {d:.1f} ثانية، {fps} فريم في الثانية')
        if real:
            v = VOICE; okv = v.get('sync_max', 9) <= 0.25
            add(okv, f"تزامن الصوت مع الصورة: أقصى فرق {v.get('sync_max', 0):.2f} ثانية (موديل {v.get('model')}, {v.get('mode')}, {v.get('timing', '')})")
            ln = subprocess.run(['ffmpeg', '-hide_banner', '-i', final, '-vn', '-af', 'loudnorm=print_format=json', '-f', 'null', '-'], capture_output=True, text=True).stderr
            i = re.search(r'"input_i"\s*:\s*"(-?[0-9.]+)"', ln); lu = float(i.group(1)) if i else -99
            add(-17 <= lu <= -12, f'مستوى الصوت {lu:.1f} LUFS')
        else:
            add(False, 'الفيديو من غير فويس أوفر')
        if reel:
            add(ASSETS['music'] == 'yes', 'موسيقى خلفية: ' + ('موجودة' if ASSETS['music'] == 'yes' else 'مش موجودة'))
            w, k = ASSETS['images_wanted'], ASSETS['images_ok']
            src = '، '.join(f'{n}: {c}' for n, c in (ASSETS.get('providers') or {}).items())
            weak = (ASSETS.get('providers') or {}).get('pollinations', 0)
            add((w == 0 or k == min(w, IMG_MAX)) and not weak and (STYLE != 'draw' or bool(os.environ.get('CE_MOCK')) or ASSETS.get('clips', 0) >= k), (f'الرسومات: {k} من {w} اتولدوا ({src})' + (f'، منهم {weak} من المولّد الضعيف' if weak else '') + (f'، حركة حقيقية في {ASSETS.get("clips", 0)}' + (' (حصة التحريك خلصت والباقي ثابت بزووم بس: الريل ده مش بمستوى الستايل المعتمد)' if ce_i2v.STOP['why'] else '') if STYLE == 'draw' else '')) if w else 'الصور: الريل ما طلبش صور')
        lay = LAYOUT or {}
        for key, name in (('off_frame', 'عناصر خارجة بره الفريم'), ('overflow', 'نص مقصوص جوه مكوّن'), ('overlap', 'الفيجوال داخل على الكابشن')):
            items = lay.get(key) or []
            add(not items, f'{name}: {len(items)}' + (' (' + ' | '.join(str(x) for x in items[:4]) + ')' if items else ''))
    except Exception as e:
        add(False, f'المراجعة الفنية وقفت: {e}')
    rep = {'pass': all(x['ok'] for x in L), 'checks': L, 'voice': VOICE, 'scenes': len(timing)}
    json.dump(rep, open(f'{out}/{code}_qa.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('QA', 'PASS' if rep['pass'] else 'FAIL'); [print(' ', 'ok ' if x['ok'] else 'BAD', x['t']) for x in L]

qa_report()

# ---------- a reviewer that watches and listens: the finished mp4 goes to Gemini with its sound ----------
def watch_review():
    key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not (key and reel and os.path.exists(final)): return
    try:
        small = f'{out}/_rev.mp4'
        ff('-i', final, '-vf', 'scale=540:-2', '-r', '24', '-c:v', 'libx264', '-crf', '30', '-c:a', 'aac', '-b:a', '96k', small)
        vid = base64.b64encode(open(small, 'rb').read()).decode()
        script = ' | '.join(s.get('vo', '').replace('*', '') for s in scenes)
        ask = ('You are a strict senior video editor and voice director reviewing a short vertical social media ad for an Egyptian audience. Watch the whole video AND listen to its audio. '
               'The intended voice-over script (Egyptian Arabic) is: ' + script + ' . '
               'Judge like a picky client: 1) VOICE: is it natural Egyptian colloquial Arabic or stiff formal Arabic (fusha)? list every word that is mispronounced or sounds wrong, is the delivery lively or flat, is the pace right. '
               '2) BACKGROUND: does the background fit the style: a flat solid colour scene (paper, black or one brand colour) is a deliberate style here and is correct, so never ask to replace it with a picture or a gradient; only flag a background that hurts the contrast of the text. 3) VISUALS: any moment where text is unreadable, cut, overlapping, too small, or the screen is mostly empty. '
               '4) SYNC: do the on-screen words match what is said at that moment. 5) SOUND: music and sound effects level against the voice. '
               'Answer in Egyptian Arabic inside this exact JSON: {"ok": true or false, "score": 0-10, "voice": {"score": 0-10, "dialect": "egyptian|fusha|mixed", "wrong_words": [{"said": "", "should": "", "at": "seconds"}], "delivery": ""}, '
               '"background": {"score": 0-10, "notes": ""}, "visual": [{"at": "seconds", "problem": ""}], "sync": "", "sound": "", "fixes": ["the concrete changes the editor must make, most important first"]}. '
               'ok is true only when you would publish it as is (score 8 or more and no wrong words).')
        body = json.dumps({'contents': [{'parts': [{'inline_data': {'mime_type': 'video/mp4', 'data': vid}}, {'text': ask}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2}}).encode()
        rep, used = None, ''
        for model in ('gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite'):
            try:
                req = urllib.request.Request(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}', data=body, headers={'Content-Type': 'application/json'})
                r = json.loads(urllib.request.urlopen(req, timeout=240).read())
                t = ''.join(p.get('text', '') for p in r['candidates'][0]['content']['parts'])
                rep = json.loads(t[t.index('{'):t.rindex('}') + 1]); used = model; break
            except Exception as e:
                msg = e.read().decode('utf8', 'replace')[:200] if hasattr(e, 'read') else str(e)[:200]
                print(f'watch review [{model}] failed:', msg)
        if rep is None: return
        rep['model'] = used
        # second pass on the clean voice alone: word by word, how it was said against how a Cairo native says it
        vo = f'{out}/_vo.wav'
        if os.path.exists(vo):
            try:
                ff('-i', vo, '-ac', '1', '-ar', '16000', '-b:a', '48k', f'{out}/_vo16.mp3')
                aud = base64.b64encode(open(f'{out}/_vo16.mp3', 'rb').read()).decode()
                ask2 = ('This is a voice-over recording in Egyptian colloquial Arabic (Cairo). The script it should say is: ' + script + ' . '
                        'Listen carefully and go through the script word by word, in order. For every word write how it was ACTUALLY pronounced in the recording with full tashkeel, and how a native Cairo speaker would say that word in this sentence with full tashkeel. '
                        'Mark ok false only when the recording clearly differs: a wrong vowel, a missing shadda, a swallowed or wrong letter, a wrong stress that changes the word, a formal (fusha) reading of a colloquial word, or a word skipped or replaced. Do not guess from the script: judge the sound. '
                        'Return only JSON: {"words": [{"w": "the script word", "heard": "", "native": "", "ok": true}]}')
                body2 = json.dumps({'contents': [{'parts': [{'inline_data': {'mime_type': 'audio/mp3', 'data': aud}}, {'text': ask2}]}], 'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0}}).encode()
                for model in ('gemini-3.8-flash', 'gemini-3.6-flash'):
                    try:
                        req = urllib.request.Request(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}', data=body2, headers={'Content-Type': 'application/json'})
                        r = json.loads(urllib.request.urlopen(req, timeout=180).read())
                        t = ''.join(p.get('text', '') for p in r['candidates'][0]['content']['parts'])
                        ws = json.loads(t[t.index('{'):t.rindex('}') + 1]).get('words', [])
                        bad = [x for x in ws if x.get('ok') is False and x.get('w')]
                        rep.setdefault('voice', {})['heard_check'] = {'model': model, 'words': len(ws), 'wrong': len(bad)}
                        if bad:
                            rep['voice']['wrong_words'] = (rep['voice'].get('wrong_words') or []) + [{'said': x.get('heard', ''), 'should': x.get('native', ''), 'word': x['w']} for x in bad]
                            rep['ok'] = False
                            rep['fixes'] = ['الكلمة «%s» اتنطقت «%s» والصح «%s»: اكتبها في data-vo بالتشكيل الصح ده عشان تتنطق مظبوط' % (x['w'], x.get('heard', ''), x.get('native', '')) for x in bad] + (rep.get('fixes') or [])
                        print('HEARD CHECK', model, len(ws), 'words,', len(bad), 'wrong')
                        break
                    except Exception as e:
                        msg = e.read().decode('utf8', 'replace')[:200] if hasattr(e, 'read') else str(e)[:200]
                        print(f'heard check [{model}] failed:', msg)
            except Exception as e:
                print('heard check stopped:', str(e)[:200])
        json.dump(rep, open(f'{out}/{code}_review.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('WATCH REVIEW', used, 'ok' if rep.get('ok') else 'NOT OK', rep.get('score'))
    except Exception as e:
        print('watch review stopped:', str(e)[:200])

watch_review()
for f in os.listdir(out):
    if f.startswith('_'): os.remove(os.path.join(out, f))
print('mp4 done', final, 'voice' if real else 'NO VOICE (silent)')
