#!/usr/bin/env python3
"""Reel agent helper: lays out design.html with estimated timings (no voice, no cost), takes two frames per scene
(entrance and settled) into one contact sheet, and prints the layout checks. Usage: python reel_look.py"""
import glob, json, os, re, subprocess
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
css = open(os.path.join(HERE, 'kit.css'), encoding='utf-8').read()
js = open(os.path.join(HERE, 'kit.js'), encoding='utf-8').read()
html = open('design.html', encoding='utf-8').read()
html = re.sub(r'<link[^>]+kit\.css[^>]*>', '', html)
html = re.sub(r'<script[^>]+kit\.js[^>]*>\s*</script>', '', html)
inject = '<style>' + css + '</style><script>window.CE_RENDER=1</script><script>' + js + '</script>'
html = html.replace('<head>', '<head>' + inject, 1) if '<head>' in html else inject + html
os.makedirs('preview', exist_ok=True)
for f in glob.glob('preview/f*.jpg'):
    os.remove(f)
with sync_playwright() as p:
    kw = {'executable_path': os.environ['CHROME']} if os.environ.get('CHROME') else {}
    b = p.chromium.launch(args=['--no-sandbox', '--font-render-hinting=none'], **kw)
    pg = b.new_page(viewport={'width': 1080, 'height': 1920})
    pg.set_content(html, wait_until='networkidle')
    pg.evaluate('document.fonts.ready'); pg.evaluate('window.ceApply()'); pg.wait_for_timeout(400)
    pg.evaluate('document.fonts.ready'); pg.evaluate('window.ceFit()')
    T = pg.evaluate('[...document.querySelectorAll(".scene")].map(s => [parseFloat(getComputedStyle(s).getPropertyValue("--s")) || 0, parseFloat(getComputedStyle(s).getPropertyValue("--d")) || 2, s.dataset.vo || ""])')
    pg.evaluate('document.getAnimations().forEach(a => a.pause())')
    k = 0
    for s, d, vo in T:
        for f in (0.3, 0.9):
            pg.evaluate('t => document.getAnimations().forEach(a => { a.currentTime = t })', (s + d * f) * 1000)
            pg.wait_for_timeout(50)
            pg.screenshot(path='preview/f%02d.jpg' % k, type='jpeg', quality=80); k += 1
    qa = pg.evaluate('window.ceQA ? window.ceQA() : {}')
    b.close()
cols = 6
rows = (k + cols - 1) // cols
subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', 'preview/f%02d.jpg', '-vf', f'scale=240:-1,tile={cols}x{rows}:padding=6:color=white', '-frames:v', '1', 'preview/sheet.jpg'], check=True)
print('scenes (two frames each, read left to right in rows of 6: entrance then settled):')
for i, (s, d, vo) in enumerate(T, 1):
    print(f'  {i}: {vo}')
print('layout checks:', json.dumps(qa, ensure_ascii=False)[:1500])
print('now Read preview/sheet.jpg (and preview/fNN.jpg for a full-size frame)')
