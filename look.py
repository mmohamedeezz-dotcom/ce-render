#!/usr/bin/env python3
"""Design agent helper: puts design.html back into spec.json, renders it, and reports what to look at.
Images are cached by their data-img prompt, so a re-render costs nothing unless a prompt changed."""
import json, os, subprocess, sys

spec = json.load(open('spec.json', encoding='utf-8'))
spec['html'] = open('design.html', encoding='utf-8').read()
json.dump(spec, open('spec.json', 'w', encoding='utf-8'), ensure_ascii=False)
code = ''.join(c for c in str(spec.get('code') or 'asset') if c.isalnum() or c in '-_') or 'asset'
r = subprocess.run([sys.executable, 'render.py', 'spec.json', 'out'], capture_output=True, text=True)
tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
png = f'out/{code}.png'
print('render', 'ok' if r.returncode == 0 and os.path.exists(png) else 'FAILED')
for line in tail:
    print('  ', line[:200])
print('now Read this file to see the result:', png)
