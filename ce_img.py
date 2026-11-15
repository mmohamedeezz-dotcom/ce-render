"""CE image tools: free illustration generation + depth layers for 2.5D parallax.
Providers, in order: Cloudflare Workers AI (if CF_ACCOUNT_ID + CF_API_TOKEN are set), the public FLUX.1 [schnell]
demo Space on Hugging Face (no key), then Pollinations (no key, weakest).
"""
import urllib.error
import base64, hashlib, json, os, subprocess, time, urllib.parse, urllib.request

CACHE = os.environ.get('CE_CACHE', 'cache'); os.makedirs(CACHE, exist_ok=True)
SPACE = 'https://black-forest-labs-flux-1-schnell.hf.space/gradio_api'
DEPTH_URL = 'https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model_quantized.onnx'
USED = {}


def _get(url, data=None, headers=None, timeout=150):
    h = dict({'User-Agent': 'ce-render/1.0'}, **(headers or {}))
    if 'hf.space' in url and os.environ.get('HF_TOKEN', '').strip(): h['Authorization'] = 'Bearer ' + os.environ['HF_TOKEN'].strip()
    req = urllib.request.Request(url, data=data, headers=h)
    return urllib.request.urlopen(req, timeout=timeout).read()


def _to_jpg(raw, dest):
    tmp = dest + '.src'
    open(tmp, 'wb').write(raw)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', tmp, '-q:v', '2', dest], check=True)
    os.remove(tmp)
    return os.path.getsize(dest) > 20000


def _cloudflare(prompt, w, h, seed, dest):
    acct, tok = os.environ.get('CF_ACCOUNT_ID', '').strip(), os.environ.get('CF_API_TOKEN', '').strip()
    if not (acct and tok): return False
    url = f'https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/black-forest-labs/flux-1-schnell'
    hd = {'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'}
    r = None
    for body in ({'prompt': prompt[:2000], 'steps': 4, 'seed': int(seed)}, {'prompt': prompt[:2000], 'steps': 4}, {'prompt': prompt[:2000]}):
        try:
            r = json.loads(_get(url, json.dumps(body).encode(), hd)); break
        except urllib.error.HTTPError as e:
            msg = e.read().decode('utf8', 'replace')[:300]
            print(f'cloudflare {e.code} with {sorted(body)}:', msg)
            if e.code != 400: raise RuntimeError(f'HTTP {e.code}: {msg[:120]}')
    if r is None: raise RuntimeError('cloudflare rejected every request shape')
    img = (r.get('result') or {}).get('image')
    return bool(img) and _to_jpg(base64.b64decode(img), dest)


def _hf_space(prompt, w, h, seed, dest):
    w, h = (768, 1344) if h > w else (1024, 1024)
    ev = json.loads(_get(SPACE + '/call/infer', json.dumps({'data': [prompt, seed, False, w, h, 4]}).encode(), {'Content-Type': 'application/json'}))
    out = _get(SPACE + '/call/infer/' + ev['event_id'], timeout=180).decode('utf8', 'replace')
    if 'event: complete' not in out: raise RuntimeError(out.strip()[-200:])
    data = json.loads(out.split('event: complete', 1)[1].split('data:', 1)[1].strip().split('\n')[0])
    return _to_jpg(_get(data[0]['url']), dest)


def _pollinations(prompt, w, h, seed, dest):
    url = 'https://image.pollinations.ai/prompt/' + urllib.parse.quote(prompt) + f'?width={w}&height={h}&model=flux&nologo=true&seed={seed}'
    raw = _get(url)
    time.sleep(16)
    return raw[:2] == b'\xff\xd8' and _to_jpg(raw, dest)


def fetch_image(prompt, w=1080, h=2048, seed=1):
    """returns a jpg path or None; every prompt is generated once and cached."""
    f = os.path.join(CACHE, hashlib.sha1(f'{prompt}|{w}|{h}|{seed}'.encode()).hexdigest() + '.jpg')
    if os.path.exists(f) and os.path.getsize(f) > 20000: return f
    if os.environ.get('CE_TESTIMGS'):  # test hook: use ready pictures in order
        L = os.environ['CE_TESTIMGS'].split(','); USED['test'] = USED.get('test', 0) + 1; return L[(USED['test'] - 1) % len(L)]
    if os.environ.get('CE_MOCK'):
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'gradients=s=768x1344:c0=0x5b3a1e:c1=0xd9a441:x0=0:y0=0:x1=768:y1=1344', '-frames:v', '1', '-q:v', '2', f], check=True); return f
    for name, fn, tries in (('cloudflare', _cloudflare, 2), ('flux space', _hf_space, 3), ('pollinations', _pollinations, 2)):
        for k in range(tries):
            try:
                if fn(prompt, w, h, seed, f):
                    USED[name] = USED.get(name, 0) + 1; return f
                if name == 'cloudflare': break  # not configured
            except Exception as e:
                print(f'image [{name}] try {k + 1} failed:', str(e)[:160]); time.sleep(6 + 8 * k)
    return None


_sess = None

def _depth(img):
    """relative depth (bigger = nearer) with Depth-Anything-V2-Small on the CPU."""
    global _sess
    import numpy as np, cv2, onnxruntime as ort
    if _sess is None:
        m = os.path.join(CACHE, 'depth_small.onnx')
        if not (os.path.exists(m) and os.path.getsize(m) > 5_000_000): open(m, 'wb').write(_get(DEPTH_URL, timeout=300))
        _sess = ort.InferenceSession(m, providers=['CPUExecutionProvider'])
    H, W = img.shape[:2]; s = 518 / max(H, W)
    h, w = max(14, int(round(H * s / 14)) * 14), max(14, int(round(W * s / 14)) * 14)
    x = cv2.cvtColor(cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    x = ((x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)).transpose(2, 0, 1)[None]
    d = _sess.run(None, {_sess.get_inputs()[0].name: x})[0].squeeze()
    d = (d - d.min()) / max(1e-6, float(d.max() - d.min()))
    return cv2.resize(d, (W, H), interpolation=cv2.INTER_CUBIC)


def make_layers(jpg):
    """split a picture into a filled-in background and a soft-edged foreground, for real depth motion.
    returns (background.jpg, foreground.png) or None when the picture has no clear subject."""
    try:
        import numpy as np, cv2
        bgf, fgf = jpg[:-4] + '_bg.jpg', jpg[:-4] + '_fg.png'
        if os.path.exists(bgf) and os.path.exists(fgf): return bgf, fgf
        img = cv2.imread(jpg); d = _depth(img)
        d8 = (d * 255).astype(np.uint8)
        thr, _ = cv2.threshold(d8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mask = (d8 > thr).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        share = float(mask.mean()) / 255
        if not 0.08 <= share <= 0.8: return None
        grow = cv2.dilate(mask, np.ones((31, 31), np.uint8))
        small = cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        fill = cv2.inpaint(small, cv2.resize(grow, (small.shape[1], small.shape[0])), 7, cv2.INPAINT_TELEA)
        fill = cv2.GaussianBlur(cv2.resize(fill, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_CUBIC), (0, 0), 3)
        keep = cv2.GaussianBlur(grow, (0, 0), 12).astype(np.float32)[..., None] / 255
        bg = (fill * keep + img * (1 - keep)).astype(np.uint8)
        alpha = cv2.GaussianBlur(mask, (0, 0), 5)
        cv2.imwrite(bgf, bg, [cv2.IMWRITE_JPEG_QUALITY, 90])
        cv2.imwrite(fgf, np.dstack([img, alpha]))
        return bgf, fgf
    except Exception as e:
        print('depth layers failed:', str(e)[:160]); return None


def data_uri(path):
    mime = 'image/png' if path.endswith('.png') else 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(open(path, 'rb').read()).decode()


if __name__ == '__main__':  # python ce_img.py selftest -> one Cloudflare image, prints what happened
    import sys
    p = 'Vintage 1960s comic book illustration, bold black ink outlines, flat warm colours in orange, teal and brown, aged paper texture, full bleed with no border and no frame. A man holding a coffee mug in a quiet kitchen at dawn.'
    try:
        ok = _cloudflare(p, 1080, 2048, 4242, 'selftest.jpg')
        print('cloudflare selftest:', 'OK ' + str(os.path.getsize('selftest.jpg')) + ' bytes' if ok else 'not configured')
    except Exception as e:
        print('cloudflare selftest FAILED:', str(e)[:300]); sys.exit(1)
