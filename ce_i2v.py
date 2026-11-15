"""CE image-to-video: turns one drawing into a short moving clip with the open Wan 2.2 I2V model.
Provider now: a public Hugging Face ZeroGPU Space (no key; HF_TOKEN raises the daily quota)."""
import json, os, shutil, subprocess, sys, time, traceback

SPACE = os.environ.get('CE_I2V_SPACE', 'zerogpu-aoti/wan2-2-fp8da-aoti-faster')
KEEP = 'Very subtle, slow, gentle hand-drawn animation, only this one small motion, everything else stays still, the vintage comic illustration style, ink outlines and colours stay exactly the same, static camera, no new objects, no text.'


def portrait(src, dest, w=576, h=1024):
    """centre-crop any picture to 9:16 so the clip comes out vertical"""
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', src, '-vf',
                    f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}', '-q:v', '2', dest], check=True)
    return dest


def animate(jpg, motion, dest, seconds=3.0, steps=4):
    """returns dest (mp4) or raises"""
    try:
        from gradio_client import Client, handle_file
    except ImportError:  # the render workflow does not list it; install on first use
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'gradio_client'], check=True)
        from gradio_client import Client, handle_file
    tok = os.environ.get('HF_TOKEN') or None
    try: c = Client(SPACE, token=tok, verbose=False)
    except TypeError: c = Client(SPACE, hf_token=tok, verbose=False)
    res = c.predict(input_image=handle_file(jpg), prompt=motion + ' ' + KEEP, steps=steps,
                    duration_seconds=seconds, api_name='/generate_video')
    v = res[0] if isinstance(res, (list, tuple)) else res
    path = v.get('video') if isinstance(v, dict) else v
    if isinstance(path, dict): path = path.get('path') or path.get('url')
    shutil.copy(path, dest)
    return dest


def sprite(mp4, dest, n=49, w=468, h=832):
    """49 frames of the clip as one 7x7 sheet, cropped to exact 9:16"""
    fr = int(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries', 'stream=nb_read_frames',
                             '-of', 'csv=p=0', mp4], capture_output=True, text=True).stdout.strip() or 0)
    if fr < n: raise RuntimeError(f'clip has only {fr} frames')
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', mp4, '-vf',
                    f"select='lt(n\\,{n})',scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},tile=7x7", '-frames:v', '1', '-q:v', '3', dest], check=True)
    return dest


STOP = {'why': ''}  # once the provider says the quota is gone, stop asking for the rest of this render

def clip_sprite(jpg, motion, cache='cache'):
    """still drawing + one motion sentence -> sprite sheet path, or None (the caller keeps the still)"""
    import hashlib
    key = hashlib.sha1((open(jpg, 'rb').read()[:200000] + motion.encode())).hexdigest()
    dest = os.path.join(cache, 'clip_' + key + '.jpg')
    if os.path.exists(dest) and os.path.getsize(dest) > 50000: return dest
    L = [p for p in os.environ.get('CE_TESTCLIPS', '').split(',') if p]
    if L:  # test hook: ready clips instead of the model
        STOP['n'] = STOP.get('n', 0) + 1; return sprite(L[(STOP['n'] - 1) % len(L)], dest)
    if STOP['why'] or os.environ.get('CE_NO_I2V') or os.environ.get('CE_MOCK'): return None
    tmp = dest + '.in.jpg'; mp4 = dest + '.mp4'
    try:
        portrait(jpg, tmp); animate(tmp, motion, mp4); return sprite(mp4, dest)
    except Exception as e:
        msg = str(e)[:200]; print('i2v failed:', msg)
        if 'quota' in msg.lower() or 'limit' in msg.lower(): STOP['why'] = msg
        return None
    finally:
        for f in (tmp, mp4):
            if os.path.exists(f): os.remove(f)


if __name__ == '__main__':  # python ce_i2v.py selftest out
    import ce_img
    out = sys.argv[2] if len(sys.argv) > 2 else 'out'; os.makedirs(out, exist_ok=True)
    log = []
    try:
        from gradio_client import Client
        d = Client(SPACE, verbose=False).view_api(return_format='dict')
        log.append('API: ' + json.dumps({k: [(p.get('parameter_name'), str(p.get('parameter_default'))[:30]) for p in v.get('parameters', [])] for k, v in d.get('named_endpoints', {}).items()}, ensure_ascii=False)[:1500])
    except Exception as e:
        log.append('view_api failed: ' + str(e)[:300])
    S = ce_img_style = 'Vintage 1960s comic book illustration, bold black ink outlines, flat warm colours in orange, teal and brown, aged paper texture, full bleed with no border and no frame. '
    tests = [
        ('man', 'A young man in a sweater standing by a window at sunrise holding a steaming coffee mug with both hands, eyes closed, calm smile.',
         'The man slowly turns his head toward the window and lifts the mug for a sip while steam rises and drifts.'),
        ('clock', 'An old wooden wall clock with roman numerals hanging on a plain wall, morning light.',
         'The clock hands tick forward and the pendulum swings gently left and right.'),
        ('beans', 'Roasted coffee beans pouring from a scoop into a metal bowl, close up.',
         'Coffee beans keep falling into the bowl and bounce, a thin wisp of smoke rises.'),
    ]
    for name, img, motion in tests:
        t = time.time()
        try:
            raw = os.path.join(out, name + '_sq.jpg')
            if not ce_img._cloudflare(S + img + ' No text, no letters, no numbers.', 1080, 2048, 1, raw): raise RuntimeError('cloudflare not configured')
            still = portrait(raw, os.path.join(out, name + '.jpg')); os.remove(raw)
            animate(still, motion, os.path.join(out, name + '.mp4'))
            log.append(f'{name}: OK in {time.time() - t:.0f}s')
        except Exception as e:
            log.append(f'{name}: FAILED after {time.time() - t:.0f}s: {str(e)[:500]} | {traceback.format_exc()[-400:]}')
        print(log[-1])
    open(os.path.join(out, 'log.txt'), 'w').write('\n'.join(log))
