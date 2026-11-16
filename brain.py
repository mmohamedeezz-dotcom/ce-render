#!/usr/bin/env python3
"""
العقل المدبر — long video -> short reels.

  transcribe AUDIO OUT.json            Groq Whisper, word timestamps
  pick TRANSCRIPT.json OUT.json        Claude reads the transcript, shortlists candidates
  judge CANDS.json CLIPS_DIR OUT.json  Claude looks at frames of each candidate, keeps the best N
  render CLIP.mp4 PICK.json OUT.mp4    9:16 face-tracked reframe + word captions + hook

Env: GROQ_API_KEY, ANTHROPIC_API_KEY, CE_GUIDE (agent instructions from n8n), CE_N (reels wanted)
"""
import base64, json, math, os, re, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get('CE_MODEL', 'claude-sonnet-5-5')
N = int(os.environ.get('CE_N', '10') or 10)
MIN_S, MAX_S = 20.0, 90.0


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def dur(path):
    o = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                       capture_output=True, text=True, check=True).stdout
    return float(o.strip())


# ----------------------------------------------------------------------------- transcribe
def transcribe(audio, out):
    import requests
    from concurrent.futures import ThreadPoolExecutor
    key = os.environ['GROQ_API_KEY']
    total = dur(audio)
    step = 600.0
    tmp = Path('chunks'); tmp.mkdir(exist_ok=True)
    parts = []
    t = 0.0
    while t < total:
        p = tmp / f'c{int(t):06d}.mp3'
        sh(['ffmpeg', '-v', 'error', '-y', '-ss', str(t), '-t', str(step), '-i', str(audio),
            '-ac', '1', '-ar', '16000', '-b:a', '32k', str(p)])
        parts.append((t, p)); t += step

    def one(item):
        off, p = item
        for attempt in range(5):
            with open(p, 'rb') as f:
                r = requests.post('https://api.groq.com/openai/v1/audio/transcriptions',
                                  headers={'Authorization': f'Bearer {key}'},
                                  files={'file': (p.name, f, 'audio/mpeg')},
                                  data=[('model', os.environ.get('CE_ASR', 'whisper-large-v3')), ('language', 'ar'),
                                        ('response_format', 'verbose_json'), ('temperature', '0'),
                                        ('timestamp_granularities[]', 'word'),
                                        ('timestamp_granularities[]', 'segment')], timeout=300)
            if r.status_code == 200:
                j = r.json()
                words = [{'w': w['word'].strip(), 's': round(w['start'] + off, 2), 'e': round(w['end'] + off, 2)}
                         for w in j.get('words') or [] if w.get('word', '').strip()]
                segs = [{'s': round(s['start'] + off, 2), 'e': round(s['end'] + off, 2), 't': s['text'].strip()}
                        for s in j.get('segments') or [] if s.get('text', '').strip()]
                return words, segs
            print('groq', r.status_code, r.text[:300], file=sys.stderr)
            if r.status_code in (429, 500, 502, 503):
                time.sleep(8 * (attempt + 1)); continue
            r.raise_for_status()
        raise SystemExit('groq: gave up on ' + p.name)

    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, parts))
    words = [w for r in res for w in r[0]]
    segs = [s for r in res for s in r[1]]
    Path(out).write_text(json.dumps({'duration': total, 'words': words, 'segments': segs}, ensure_ascii=False))
    print(f'transcribed {total:.0f}s -> {len(words)} words, {len(segs)} segments in {time.time() - t0:.0f}s')


# ----------------------------------------------------------------------------- claude
def claude(system, content, max_tokens=32000):
    import requests
    for attempt in range(3):
        r = requests.post('https://api.anthropic.com/v1/messages',
                          headers={'x-api-key': os.environ['ANTHROPIC_API_KEY'], 'anthropic-version': '2023-06-01',
                                   'content-type': 'application/json'},
                          json={'model': MODEL, 'max_tokens': max_tokens, 'system': system,
                                'messages': [{'role': 'user', 'content': content}]}, timeout=600)
        if r.status_code == 200:
            j = r.json()
            text = ''.join(b.get('text', '') for b in j['content'] if b.get('type') == 'text')
            print('claude:', j.get('stop_reason'), j.get('usage'), [b.get('type') for b in j['content']], file=sys.stderr)
            if text.strip():
                return text
            time.sleep(3); continue
        print('claude', r.status_code, r.text[:300], file=sys.stderr)
        if r.status_code in (429, 500, 529):
            time.sleep(15 * (attempt + 1)); continue
        r.raise_for_status()
    raise SystemExit('claude: gave up')


def jparse(text):
    m = re.search(r'\[.*\]', text, re.S)
    if not m:
        raise SystemExit('no JSON array in model output:\n' + text[:800])
    return json.loads(m.group(0))


def mmss(t):
    return f'{int(t // 60):02d}:{t % 60:04.1f}'


GUIDE = """أنت «العقل المدبر»: مونتير ومحرر محتوى محترف. شغلتك تطلّع من حلقة طويلة مقاطع قصيرة (ريلز) تتشاف للآخر.
المقطع القوي:
- بيبدأ بجملة تشد من أول ثانيتين (سؤال صادم، اعتراف، رقم، رأي عكس المتوقع، وعد بفايدة). ممنوع يبدأ بتمهيد أو «طيب» أو «يعني» أو نص جملة.
- مفهوم لوحده من غير ما حد يشوف باقي الحلقة. مفيش «زي ما قلت» أو ضمير مرجعه بره المقطع.
- فيه فكرة واحدة كاملة وبيخلص على قفلة: نتيجة، حكمة، ضحكة أو جملة تتكرر. ممنوع يتقطع في نص فكرة.
- مدته بين 25 و75 ثانية. الأقصر أحسن لو الفكرة كملت.
ابعد عن: الترحيب، التعريف بالضيف، الإعلانات والرعاة، الكلام اللوجستي، والمقاطع اللي معناها متوقف على صورة أو سياق سابق."""


def pick(tr_path, out):
    tr = json.loads(Path(tr_path).read_text())
    guide = os.environ.get('CE_GUIDE') or GUIDE
    lines = '\n'.join(f"[{mmss(s['s'])}] {s['t']}" for s in tr['segments'])
    want = max(N + 6, int(N * 1.6))
    system = guide + f"""

هتستلم تفريغ الحلقة، كل سطر قدامه توقيته [دقيقة:ثانية]. اختار أقوى {want} مقطع مرشّح، ومتكررش نفس الفكرة مرتين.
رجّع JSON array بس، من غير أي كلام قبله أو بعده. كل عنصر:
{{"start": ثواني بداية أول سطر في المقطع, "end": ثواني نهاية آخر جملة, "hook": "عنوان يتكتب فوق الفيديو، 3 لـ6 كلمات بالعامية المصرية، يثير الفضول من غير ما يحرق القفلة", "first": "أول 5 كلمات بالظبط زي ما اتقالت", "last": "آخر 5 كلمات بالظبط", "why": "سبب الاختيار في جملة", "score": من 1 لـ100}}
الوقت بالثواني من أول الحلقة (مثال: [12:30.0] = 750). start و end لازم يكونوا على حدود جمل كاملة."""
    cands = jparse(claude(system, f'مدة الحلقة: {mmss(tr["duration"])}\n\n{lines}'))
    words = tr['words']
    good = []
    for c in cands:
        try:
            s, e = float(c['start']), float(c['end'])
        except Exception:
            continue
        s, e = snap(words, s, e, c.get('first', ''), c.get('last', ''))
        if e - s < MIN_S or e - s > MAX_S:
            continue
        if any(not (e <= g['start'] or s >= g['end']) for g in good):
            continue
        good.append({'id': len(good) + 1, 'start': round(s, 2), 'end': round(e, 2), 'hook': c.get('hook', '').strip(),
                     'why': c.get('why', ''), 'score': c.get('score', 0),
                     'text': ' '.join(w['w'] for w in words if s - .01 <= w['s'] < e)})
    good.sort(key=lambda g: -float(g.get('score') or 0))
    for i, g in enumerate(good):
        g['id'] = i + 1
    Path(out).write_text(json.dumps(good, ensure_ascii=False, indent=1))
    print(f'{len(cands)} proposed -> {len(good)} valid candidates')
    for g in good:
        print(f"  #{g['id']} {mmss(g['start'])}-{mmss(g['end'])} ({g['end'] - g['start']:.0f}s) [{g['score']}] {g['hook']}")


def norm(s):
    s = re.sub(r'[ً-ْـ]', '', s)
    s = re.sub(r'[إأآا]', 'ا', s).replace('ى', 'ي').replace('ة', 'ه')
    return re.sub(r'[^\w\s]', '', s).strip()


def snap(words, s, e, first, last):
    """Move start/end onto real word boundaries, preferring the exact words the model quoted."""
    if not words:
        return s, e

    def find(quote, around, win, take_end):
        q = norm(quote).split()[:5] if not take_end else norm(quote).split()[-5:]
        if len(q) < 2:
            return None
        best = None
        for i, w in enumerate(words):
            if abs(w['s'] - around) > win:
                continue
            seq = [norm(x['w']) for x in words[i:i + len(q)]]
            hit = sum(a == b for a, b in zip(seq, q))
            if hit >= max(2, len(q) - 1) and (best is None or hit > best[0]):
                best = (hit, i)
        if best is None:
            return None
        i = best[1]
        return words[i]['s'] if not take_end else words[min(i + len(q), len(words)) - 1]['e']

    s2 = find(first, s, 12, False)
    e2 = find(last, e, 12, True)
    if s2 is None:
        s2 = min(words, key=lambda w: abs(w['s'] - s))['s']
    if e2 is None:
        e2 = min(words, key=lambda w: abs(w['e'] - e))['e']
    return max(0.0, s2 - 0.12), e2 + 0.25


# ----------------------------------------------------------------------------- judge
def sheet(clip, dest, n=6):
    d = dur(clip)
    sh(['ffmpeg', '-v', 'error', '-y', '-i', str(clip), '-vf',
        f"fps={n}/{max(d, 1):.3f},scale=320:-2,tile={n // 2}x2", '-frames:v', '1', '-q:v', '4', str(dest)])


def judge(cands_path, clips_dir, out):
    cands = json.loads(Path(cands_path).read_text())
    content, have = [], []
    for c in cands:
        clip = Path(clips_dir) / f"c{c['id']}.mp4"
        if not clip.exists() or clip.stat().st_size < 20000:
            print('missing clip', clip.name); continue
        jpg = Path(clips_dir) / f"c{c['id']}.jpg"
        sheet(clip, jpg)
        have.append(c)
        content.append({'type': 'text', 'text': f"مقطع #{c['id']} ({c['end'] - c['start']:.0f} ث) — هوك مقترح: {c['hook']}\nالكلام: {c['text']}"})
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                                    'data': base64.b64encode(jpg.read_bytes()).decode()}})
    if not have:
        raise SystemExit('no clips to judge')
    guide = os.environ.get('CE_GUIDE') or GUIDE
    system = guide + f"""

قدامك المقاطع المرشّحة: نص كل مقطع و6 فريمات منه بالترتيب. اتفرج على الفريمات كويس وبعدين اختار أقوى {N} مقطع بس ورتّبهم من الأقوى.
ارفض أي مقطع الصورة فيه مش نافعة ريل: إعلان أو شاشة راعي، لقطة واسعة مفيش فيها متكلم واضح معظم الوقت، تتر أو شاشة سودا.
حسّن الهوك لو محتاج: 3 لـ6 كلمات بالعامية المصرية، من غير علامات تنصيص ومن غير إيموجي.
رجّع JSON array بس: [{{"id": رقم المقطع, "hook": "الهوك النهائي", "note": "ملاحظة في جملة عن الصورة"}}]"""
    picks = jparse(claude(system, content))
    by = {c['id']: c for c in have}
    final = []
    for p in picks:
        c = by.get(int(p.get('id', -1)))
        if c and c not in final:
            c['hook'] = (p.get('hook') or c['hook']).strip()
            c['note'] = p.get('note', '')
            final.append(c)
    final = final[:N]
    Path(out).write_text(json.dumps(final, ensure_ascii=False, indent=1))
    print(f'{len(have)} judged -> {len(final)} picked: ' + ', '.join(f"#{c['id']}" for c in final))


# ----------------------------------------------------------------------------- render
def probe(path):
    j = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                   'stream=width,height,r_frame_rate', '-of', 'json', str(path)],
                                  capture_output=True, text=True, check=True).stdout)['streams'][0]
    a, b = j['r_frame_rate'].split('/')
    return int(j['width']), int(j['height']), float(a) / float(b)


def track(clip, W, H, cw, afps=6.0):
    """Returns [(t, x)] crop-left positions. Locked-off shots get one static crop; moving ones a smoothed follow."""
    import cv2, numpy as np
    aw = 640; ah = int(round(H * aw / W / 2) * 2)
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-i', str(clip), '-vf', f'fps={afps},scale={aw}:{ah}',
                          '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'], stdout=subprocess.PIPE)
    det = cv2.FaceDetectorYN.create(str(HERE / 'yunet.onnx'), '', (aw, ah), 0.6, 0.3, 50)
    xs, cuts, prev, n = [], [0], None, 0
    while True:
        raw = p.stdout.read(aw * ah * 3)
        if len(raw) < aw * ah * 3:
            break
        fr = np.frombuffer(raw, np.uint8).reshape(ah, aw, 3)
        small = cv2.cvtColor(cv2.resize(fr, (64, 36)), cv2.COLOR_BGR2GRAY).astype(np.int16)
        if prev is not None and float(np.abs(small - prev).mean()) > 22:
            cuts.append(n)
        prev = small
        _, faces = det.detect(fr)
        cx = None
        if faces is not None and len(faces):
            f = max(faces, key=lambda f: f[2] * f[3] * f[14])
            if f[2] >= aw * 0.035:
                cx = (f[0] + f[2] / 2) * W / aw
        lastk = next((x for x in reversed(xs) if x is not None), None)
        if cx is not None and lastk is not None and abs(cx - lastk) > cw * 0.45 and cuts[-1] != n:
            cuts.append(n)
        xs.append(cx); n += 1
    p.wait()
    cuts.append(n)
    out, last = [], W / 2
    for a, b in zip(cuts, cuts[1:]):
        seg = xs[a:b]
        known = [x for x in seg if x is not None]
        if len(known) < max(1, len(seg) * 0.15):
            path = [last if a else W / 2] * len(seg)          # no face: keep previous framing
        else:
            arr = np.array([np.nan if x is None else x for x in seg], float)
            idx = np.arange(len(arr)); ok = ~np.isnan(arr)
            arr = np.interp(idx, idx[ok], arr[ok])
            lo, hi = np.percentile(arr, [10, 90])
            if hi - lo < cw * 0.22:
                path = [float(np.median(arr))] * len(seg)     # locked-off shot: one steady crop
            else:
                k = int(afps * 1.2) | 1
                pad = np.pad(arr, k, mode='edge')
                g = np.exp(-0.5 * (np.arange(-k, k + 1) / (k / 2.5)) ** 2); g /= g.sum()
                sm = np.convolve(pad, g, mode='same')[k:-k]
                path, cur = [], float(sm[0])
                for v in sm:                                   # follow only when the face leaves the dead-zone
                    if abs(v - cur) > cw * 0.10:
                        cur += (v - cur) * 0.35
                    path.append(cur)
        last = path[-1]
        for i, x in enumerate(path):
            out.append(((a + i) / afps, int(round(min(max(x - cw / 2, 0), W - cw) / 2) * 2), i == 0))
    faces_seen = sum(x is not None for x in xs) / max(1, len(xs))
    return out, len(cuts) - 1, faces_seen


def ass_time(t):
    t = max(t, 0)
    return f'{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}'


def captions(words, hook, total, dest):
    """Word-by-word captions: 3-4 words on screen, the spoken one highlighted."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Tajawal ExtraBold,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,3,2,90,90,520,-1
Style: Hook,Tajawal Black,74,&H00111111,&H00111111,&H000AD6FF,&H000AD6FF,0,0,0,0,100,100,0,0,3,18,0,8,110,110,250,-1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(3.6, total))},Hook,,0,0,0,,{{\\fad(120,200)}}{hook}")
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= 4 or w['s'] - cur[-1]['e'] > 0.6 or sum(len(x['w']) for x in cur) + len(w['w']) > 22
                    or re.search(r'[.؟!?،]$', cur[-1]['w'])):
            groups.append(cur); cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    for gi, g in enumerate(groups):
        g_end = min(g[-1]['e'] + 0.25, groups[gi + 1][0]['s'] if gi + 1 < len(groups) else total)
        for i, w in enumerate(g):
            a = w['s']
            b = g[i + 1]['s'] if i + 1 < len(g) else g_end
            if b <= a:
                continue
            # one colour tag per word: libass orders tagged runs right-to-left correctly, but not words inside a run
            txt = ' '.join(('{\\c&H0AD6FF&}' if j == i else '{\\c&HFFFFFF&}') + x['w'] for j, x in enumerate(g))
            pop = '{\\fscx92\\fscy92\\t(0,90,\\fscx100\\fscy100)}' if i == 0 else ''
            ev.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{pop}{txt}")
    Path(dest).write_text(head + '\n'.join(ev) + '\n', encoding='utf-8')
    return len(groups)


def fonts_dir():
    if (HERE / 'Tajawal-ExtraBold.ttf').exists() and (HERE / 'Tajawal-Black.ttf').exists():
        return HERE
    d = HERE / 'fonts'; d.mkdir(exist_ok=True)
    for name in ('Tajawal-ExtraBold.ttf', 'Tajawal-Black.ttf'):
        f = d / name
        if not f.exists():
            sh(['curl', '-sSLf', '-o', str(f), f'https://github.com/google/fonts/raw/main/ofl/tajawal/{name}'])
    return d


def render(clip, pick_path, out):
    c = json.loads(Path(pick_path).read_text())
    W, H, fps = probe(clip)
    total = dur(clip)
    cw = int(round(H * 9 / 16 / 2) * 2)
    t0 = time.time()
    path, shots, seen = track(clip, W, H, cw)
    work = Path(out).with_suffix('')
    cmd = work.with_suffix('.cmd'); sub = work.with_suffix('.ass')
    lines, lastx = [], None
    for i, (t, x, cut) in enumerate(path):
        nxt = path[i + 1] if i + 1 < len(path) else None
        steps = 5 if (nxt and not nxt[2] and nxt[1] != x) else 1
        for k in range(steps):
            xi = x if steps == 1 else int(round((x + (nxt[1] - x) * k / steps) / 2) * 2)
            if xi != lastx:
                lines.append(f"{t + (nxt[0] - t) * k / steps if steps > 1 else t:.3f} crop x {xi};")
                lastx = xi
    cmd.write_text('\n'.join(lines) + '\n')
    words = [{'w': w['w'], 's': w['s'] - c['start'], 'e': w['e'] - c['start']} for w in c.get('words', [])]
    words = [w for w in words if w['e'] > 0 and w['s'] < total]
    g = captions(words, c.get('hook', ''), total, sub)
    x0 = path[0][1] if path else (W - cw) // 2
    esc = lambda p: str(p).replace('\\', '/').replace(':', '\\:')
    vf = (f"sendcmd=f='{esc(cmd)}',crop={cw}:{H}:{x0}:0,scale=1080:1920:flags=lanczos,"
          f"unsharp=5:5:0.6,ass='{esc(sub)}':fontsdir='{esc(fonts_dir())}'")
    sh(['ffmpeg', '-v', 'error', '-y', '-i', str(clip), '-vf', vf, '-af', 'loudnorm=I=-14:TP=-1:LRA=11',
        '-r', '30', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '160k', '-ar', '48000', '-movflags', '+faststart', str(out)])
    qa = {'id': c.get('id'), 'seconds': round(total, 1), 'source': f'{W}x{H}', 'shots': shots,
          'face_seen': round(seen, 2), 'caption_groups': g, 'words': len(words),
          'render_seconds': round(time.time() - t0, 1),
          'warn': [w for w in (('الوش ظاهر في أقل من نص المقطع' if seen < 0.5 else ''),
                               ('مفيش كلمات للكابشن' if not words else ''),
                               ('جودة المصدر أقل من 720p' if H < 720 else '')) if w]}
    work.with_suffix('.json').write_text(json.dumps(qa, ensure_ascii=False))
    print(json.dumps(qa, ensure_ascii=False))


def attach_words(tr_path, picks_path, outdir):
    """Split picks.json into one file per reel, each carrying its own words (for the render matrix)."""
    tr = json.loads(Path(tr_path).read_text())
    picks = json.loads(Path(picks_path).read_text())
    Path(outdir).mkdir(exist_ok=True)
    for c in picks:
        c['words'] = [w for w in tr['words'] if c['start'] - .01 <= w['s'] < c['end']]
        (Path(outdir) / f"p{c['id']}.json").write_text(json.dumps(c, ensure_ascii=False))
    print(json.dumps([c['id'] for c in picks]))


if __name__ == '__main__':
    a = sys.argv[1:]
    {'transcribe': transcribe, 'pick': pick, 'judge': judge, 'render': render, 'split': attach_words}[a[0]](*a[1:])
