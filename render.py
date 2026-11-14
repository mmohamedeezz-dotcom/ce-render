#!/usr/bin/env python3
"""CE renderer: HTML/CSS animation -> MP4 (frame-exact) + VO + SFX mix.
usage: render.py spec.json outdir
spec: {code, kind: reel|post, duration, fps, html, audio:{voice_id, vo:[{t,text}], sfx:[{t,prompt,dur}]}}
"""
import json, os, subprocess, sys, urllib.request
from playwright.sync_api import sync_playwright

spec = json.load(open(sys.argv[1], encoding='utf-8'))
out = sys.argv[2]; os.makedirs(out, exist_ok=True)
code = ''.join(c for c in str(spec.get('code') or 'asset') if c.isalnum() or c in '-_') or 'asset'
reel = spec.get('kind') == 'reel'
W, H = 1080, (1920 if reel else 1350)
fps = int(spec.get('fps') or 30)
dur = max(1.0, min(90.0, float(spec.get('duration') or 15)))
KEY = os.environ.get('ELEVENLABS_API_KEY', '')
audio = spec.get('audio') or {}
voice = audio.get('voice_id') or os.environ.get('ELEVENLABS_VOICE_ID', '')

def ff(*a):
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', *a], check=True)

def eleven(path, body, dest):
    req = urllib.request.Request('https://api.elevenlabs.io' + path, data=json.dumps(body).encode(),
                                 headers={'xi-api-key': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, 'wb') as f:
        f.write(r.read())

with sync_playwright() as p:
    kw = {'executable_path': os.environ['CHROME']} if os.environ.get('CHROME') else {}
    b = p.chromium.launch(args=['--no-sandbox', '--font-render-hinting=none'], **kw)
    pg = b.new_page(viewport={'width': W, 'height': H}, device_scale_factor=1)
    pg.set_content(spec['html'], wait_until='networkidle')
    pg.evaluate('document.fonts.ready')
    if not reel:
        pg.screenshot(path=f'{out}/{code}.png')
        b.close(); print('png done'); sys.exit(0)
    pg.evaluate('document.getAnimations().forEach(a => a.pause())')
    silent = f'{out}/_silent.mp4'
    enc = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', str(fps), '-c:v', 'mjpeg', '-i', '-',
                            '-c:v', 'libx264', '-preset', 'medium', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', silent],
                           stdin=subprocess.PIPE)
    n = int(round(dur * fps))
    for i in range(n):
        pg.evaluate('t => document.getAnimations().forEach(a => { a.currentTime = t })', i * 1000.0 / fps)
        enc.stdin.write(pg.screenshot(type='jpeg', quality=95))
    enc.stdin.close(); enc.wait(); b.close()

# ---- audio ----
tracks = []   # (file, t, gain, is_sfx)
for i, v in enumerate(audio.get('vo') or []):
    if KEY and voice and v.get('text'):
        f = f'{out}/_vo{i}.mp3'
        try:
            eleven(f'/v1/text-to-speech/{voice}?output_format=mp3_44100_128', {'text': v['text'], 'model_id': audio.get('model_id') or 'eleven_v3', 'language_code': 'ar'}, f)
            tracks.append((f, float(v.get('t') or 0), 1.0, False))
        except Exception as e:
            print('VO failed', i, e)
for i, s in enumerate(audio.get('sfx') or []):
    f = f'{out}/_sfx{i}.mp3'; d = max(0.5, min(10.0, float(s.get('dur') or 1)))
    ok = False
    if KEY and s.get('prompt'):
        try:
            eleven('/v1/sound-generation', {'text': s['prompt'], 'duration_seconds': d, 'prompt_influence': 0.5}, f); ok = True
        except Exception as e:
            print('SFX failed', i, e)
    if not ok:  # synthesized fallback: soft low thump
        ff('-f', 'lavfi', '-i', f'sine=frequency=90:duration={min(d,0.35)}', '-af', 'afade=t=out:st=0.05:d=0.28,volume=1.5', f)
    tracks.append((f, float(s.get('t') or 0), float(s.get('gain') or 0.6), True))

def mix(sel, dest):
    if not sel:
        return False
    ins, flt = [], []
    for k, (f, t, g, _) in enumerate(sel):
        ins += ['-i', f]
        flt.append(f'[{k}:a]aresample=44100,volume={g},adelay={int(t*1000)}:all=1[a{k}]')
    flt.append(''.join(f'[a{k}]' for k in range(len(sel))) + f'amix=inputs={len(sel)}:normalize=0:dropout_transition=0,apad,atrim=0:{dur},alimiter=limit=0.95[m]')
    ff(*ins, '-filter_complex', ';'.join(flt), '-map', '[m]', dest); return True

final = f'{out}/{code}.mp4'
if mix(tracks, f'{out}/_mix.wav'):
    ff('-i', silent, '-i', f'{out}/_mix.wav', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', final)
    mix([t for t in tracks if t[3]], f'{out}/{code}_sfx.mp3')
else:
    os.replace(silent, final)
for f in os.listdir(out):
    if f.startswith('_'):
        os.remove(os.path.join(out, f))
print('mp4 done', final)
