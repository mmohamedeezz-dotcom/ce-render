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
import base64, json, os, re, subprocess, sys, urllib.request
from playwright.sync_api import sync_playwright

spec = json.load(open(sys.argv[1], encoding='utf-8'))
out = sys.argv[2]; os.makedirs(out, exist_ok=True)
code = ''.join(c for c in str(spec.get('code') or 'asset') if c.isalnum() or c in '-_') or 'asset'
reel = spec.get('kind') == 'reel'
W, H = 1080, (1920 if reel else 1350)
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
    v3 = {'stability': 0.5, 'similarity_boost': 0.8, 'speed': 1.08}
    v2 = {'stability': 0.45, 'similarity_boost': 0.8, 'style': 0.3, 'use_speaker_boost': True, 'speed': 1.08}
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
        pg.screenshot(path=f'{out}/{code}.png'); b.close(); print('png done'); sys.exit(0)

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
if mix(tracks, f'{out}/_mix.wav', 'loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100,'):
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
        d = adur(final); add(10 <= d <= 45, f'المدة {d:.1f} ثانية، {fps} فريم في الثانية')
        if real:
            v = VOICE; okv = v.get('sync_max', 9) <= 0.25
            add(okv, f"تزامن الصوت مع الصورة: أقصى فرق {v.get('sync_max', 0):.2f} ثانية (موديل {v.get('model')}, {v.get('mode')}, {v.get('timing', '')})")
            ln = subprocess.run(['ffmpeg', '-hide_banner', '-i', final, '-vn', '-af', 'loudnorm=print_format=json', '-f', 'null', '-'], capture_output=True, text=True).stderr
            i = re.search(r'"input_i"\s*:\s*"(-?[0-9.]+)"', ln); lu = float(i.group(1)) if i else -99
            add(-17 <= lu <= -12, f'مستوى الصوت {lu:.1f} LUFS')
        else:
            add(False, 'الفيديو من غير فويس أوفر')
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
for f in os.listdir(out):
    if f.startswith('_'): os.remove(os.path.join(out, f))
print('mp4 done', final, 'voice' if real else 'NO VOICE (silent)')
