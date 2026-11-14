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
fps = int(spec.get('fps') or 30)
KEY = os.environ.get('ELEVENLABS_API_KEY', '')
voice = spec.get('voice_id') or os.environ.get('ELEVENLABS_VOICE_ID', '')
GAP, TAIL, WORD = 0.18, 1.2, 0.36

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_CSS = open(os.path.join(HERE, 'kit.css'), encoding='utf-8').read()
KIT_JS = open(os.path.join(HERE, 'kit.js'), encoding='utf-8').read()

def ff(*a):
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', *a], check=True)

def post(path, body):
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
    for i, sc in enumerate(scenes):
        f = f'{out}/_vo{i}.mp3'
        r = speak(sc['vo'], f)
        if r: real += 1; tracks.append((f, t, 1.0, False))
        dur, words = r or estimate(sc['vo'])
        d = dur + GAP + (TAIL if i == len(scenes) - 1 else 0)
        timing.append({'s': round(t, 3), 'd': round(d, 3), 'w': [round(x, 3) for x in words]})
        if sc['sfx'] and KEY:
            g = f'{out}/_sfx{i}.mp3'
            try:
                open(g, 'wb').write(post('/v1/sound-generation', {'text': sc['sfx'], 'duration_seconds': 1.0, 'prompt_influence': 0.5}))
                tracks.append((g, t, 0.45, True))
            except Exception as e:
                print('SFX failed:', e)
        t += d
    total = t
    print(f'scenes={len(scenes)} real_vo={real} total={total:.2f}s')

    # real timings into the DOM (captions, entrances, scene windows), fit every visual to its zone, then reload clean
    pg.evaluate('T => window.ceApply(T)', timing)
    pg.evaluate('document.fonts.ready'); pg.wait_for_timeout(300)
    pg.evaluate("window.ceFit(); ['ce-kit-js','ce-kit-flag'].forEach(i => { const e = document.getElementById(i); if (e) e.remove(); })")
    final_html = pg.evaluate('"<!DOCTYPE html>" + document.documentElement.outerHTML')
    pg.set_content(final_html, wait_until='networkidle'); pg.evaluate('document.fonts.ready')
    pg.evaluate('document.getAnimations().forEach(a => a.pause())')
    silent = f'{out}/_silent.mp4'
    enc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', str(fps), '-c:v', 'mjpeg', '-i', '-',
                            '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', silent], stdin=subprocess.PIPE)
    for i in range(int(round(total * fps))):
        pg.evaluate('t => document.getAnimations().forEach(a => { a.currentTime = t })', i * 1000.0 / fps)
        enc.stdin.write(pg.screenshot(type='jpeg', quality=95))
    enc.stdin.close(); enc.wait(); b.close()

def mix(sel, dest):
    if not sel: return False
    ins, flt = [], []
    for k, (f, st, g, _) in enumerate(sel):
        ins += ['-i', f]; flt.append(f'[{k}:a]aresample=44100,volume={g},adelay={int(st*1000)}:all=1[a{k}]')
    flt.append(''.join(f'[a{k}]' for k in range(len(sel))) + f'amix=inputs={len(sel)}:normalize=0:dropout_transition=0,apad,atrim=0:{total},alimiter=limit=0.95[m]')
    ff(*ins, '-filter_complex', ';'.join(flt), '-map', '[m]', dest); return True

final = f'{out}/{code}.mp4'
if mix(tracks, f'{out}/_mix.wav'):
    ff('-i', silent, '-i', f'{out}/_mix.wav', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', final)
    mix([x for x in tracks if x[3]], f'{out}/{code}_sfx.mp3')
else:
    os.replace(silent, final)
for f in os.listdir(out):
    if f.startswith('_'): os.remove(os.path.join(out, f))
print('mp4 done', final, 'voice' if real else 'NO VOICE (silent)')
