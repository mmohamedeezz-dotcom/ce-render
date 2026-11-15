"""CE image-to-video: turns one drawing into a short moving clip with the open Wan 2.2 I2V model.
Provider now: a public Hugging Face ZeroGPU Space (no key; HF_TOKEN raises the daily quota)."""
import json, os, shutil, subprocess, sys, time, traceback

SPACE = os.environ.get('CE_I2V_SPACE', 'zerogpu-aoti/wan2-2-fp8da-aoti-faster')
KEEP = 'Subtle hand-drawn animation, the vintage comic illustration style, ink outlines and colours stay exactly the same, static camera, no new objects, no text.'


def portrait(src, dest, w=576, h=1024):
    """centre-crop any picture to 9:16 so the clip comes out vertical"""
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', src, '-vf',
                    f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}', '-q:v', '2', dest], check=True)
    return dest


def animate(jpg, motion, dest, seconds=3.0, steps=4):
    """returns dest (mp4) or raises"""
    from gradio_client import Client, handle_file
    c = Client(SPACE, hf_token=(os.environ.get('HF_TOKEN') or None), verbose=False)
    res = c.predict(input_image=handle_file(jpg), prompt=motion + ' ' + KEEP, steps=steps,
                    duration_seconds=seconds, api_name='/generate_video')
    v = res[0] if isinstance(res, (list, tuple)) else res
    path = v.get('video') if isinstance(v, dict) else v
    if isinstance(path, dict): path = path.get('path') or path.get('url')
    shutil.copy(path, dest)
    return dest


if __name__ == '__main__':  # python ce_i2v.py selftest out
    import ce_img
    out = sys.argv[2] if len(sys.argv) > 2 else 'out'; os.makedirs(out, exist_ok=True)
    log = []
    try:
        from gradio_client import Client
        log.append('API: ' + json.dumps(Client(SPACE, verbose=False).view_api(return_format='dict'), ensure_ascii=False)[:3000])
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
