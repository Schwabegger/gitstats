#!/usr/bin/env python3
"""
screenshot.py — Generate obfuscated sample report and take Playwright screenshots.

Usage:
    python3 docs/screenshot.py <path-to-git-repo>

Steps:
    1. Run gitstats.py to generate a raw HTML report
    2. Obfuscate: replace author names and commit hashes (keep messages mostly intact)
    3. Take full-page screenshots of each section via Playwright
    4. Write obfuscated HTML to docs/sample_report.html

Requirements:
    pip install playwright faker
    playwright install chromium
"""

import sys
import re
import json
import hashlib
import asyncio
import subprocess
from pathlib import Path
from faker import Faker

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT  = SCRIPT_DIR.parent
TMP_DIR    = Path('/tmp/gitstats_screenshot_run')

# ── Author name generation ─────────────────────────────────────────────────────
# Each real name maps deterministically to a fake name via a seeded Faker instance.

_fake_name_cache: dict[str, str] = {}

def fake_author_name(real: str) -> str:
    if real not in _fake_name_cache:
        seed = int(hashlib.md5(f'author:{real}'.encode()).hexdigest(), 16)
        f = Faker()
        Faker.seed(seed)
        _fake_name_cache[real] = f.name()
    return _fake_name_cache[real]

# ── Commit message fixups ─────────────────────────────────────────────────────
# Only touch messages that are genuinely unintelligible or personally revealing.
# Large-LOC commits like "reset migrations" are fine as-is.

MESSAGE_FIXUPS = {
    'suizide':   'remove deprecated module',   # German word, unrelated to app context
    'mwege':     'refactor routing',            # internal branch/person name
    'stuff':     'misc fixes',
    'project':   'initial project setup',
    'naming':    'rename identifiers for clarity',
    'Revert "Revert "mwege""': 'revert routing refactor revert',
    'Revert "mwege"':          'revert routing refactor',
    # HTML-escaped variants (as they appear in the report)
    'Revert &quot;Revert &quot;mwege&quot;&quot;': 'revert routing refactor revert',
    'Revert &quot;mwege&quot;':                     'revert routing refactor',
}

# ── Sections to screenshot ────────────────────────────────────────────────────

SECTIONS = [
    ('general',       'screenshot-general.png'),
    ('activity',      'screenshot-activity.png'),
    ('authors',       'screenshot-authors.png'),
    ('contributions', 'screenshot-contributions.png'),
    ('compare',       'screenshot-compare.png'),
    ('files',         'screenshot-files.png'),
    ('lines',         'screenshot-lines.png'),
]

# ── Hash helpers ──────────────────────────────────────────────────────────────

def fake_hash(real: str) -> str:
    """Deterministic fake full hash. Same input → same output across runs."""
    return hashlib.sha1(f'salt:{real}'.encode()).hexdigest()

def fake_hash_short(real_short: str) -> str:
    return hashlib.md5(f'salt:{real_short}'.encode()).hexdigest()[:8]

def obfuscate_author(name: str) -> str:
    return fake_author_name(name)

# ── Obfuscation ───────────────────────────────────────────────────────────────

def obfuscate(html: str) -> str:
    # 1. Strip real repo path from info table
    html = re.sub(
        r'<tr><td>Repository</td><td>[^<]*</td></tr>',
        '<tr><td>Repository</td><td>/private/REDACTED_REPO</td></tr>',
        html,
    )

    # 2. Strip remote URL from subtitle line, keep timestamp
    html = re.sub(
        r'(Report generated [^·<]+·\s*)[^\s<][^<]*',
        r'\1[private repo]',
        html,
    )

    # 3. Replace author names everywhere in HTML
    # Discover real names from data-author attributes — no hardcoded map needed.
    real_names = re.findall(r'data-author="([^"]+)"', html)
    for real in dict.fromkeys(real_names):  # deduplicate, preserve order
        fake = fake_author_name(real)
        esc = re.escape(real)
        html = html.replace(f'data-author="{real}"', f'data-author="{fake}"')
        html = re.sub(rf'>\s*{esc}\s*<', f'>{fake}<', html)
        html = html.replace(f'value="{real}"', f'value="{fake}"')
        html = html.replace(f'>{real}</option>', f'>{fake}</option>')

    # 4. Replace short hashes in <code> table cells
    html = re.sub(
        r'<code>([0-9a-f]{8})</code>',
        lambda m: f'<code>{fake_hash_short(m.group(1))}</code>',
        html,
    )

    # 5. Commit message fixups in table cells
    for orig, fixed in MESSAGE_FIXUPS.items():
        html = html.replace(
            f'<td class="commit-msg">{orig}</td>',
            f'<td class="commit-msg">{fixed}</td>',
        )

    # 6. Patch the embedded JSON data blob
    def patch_data(m):
        try:
            d = json.loads(m.group(1))
        except Exception:
            return m.group(0)

        for a in d.get('topAuthors', []):
            a['name'] = obfuscate_author(a['name'])

        for a in d.get('authorDetails', []):
            a['name'] = obfuscate_author(a['name'])

        d['authorActivity'] = {
            obfuscate_author(k): v
            for k, v in d.get('authorActivity', {}).items()
        }

        d['authorCommits'] = {
            obfuscate_author(author): [
                {
                    'hash': fake_hash(c['hash']),
                    'ts': c['ts'],
                    'subject': MESSAGE_FIXUPS.get(c['subject'], c['subject']),
                }
                for c in commits
            ]
            for author, commits in d.get('authorCommits', {}).items()
        }

        # Clear GitHub base so modal links never point to real commits
        d['githubBase'] = ''

        return 'const D = ' + json.dumps(d)

    html = re.sub(r'const D = ({[\s\S]*?})(?=;)', patch_data, html)

    return html

# ── Screenshots ───────────────────────────────────────────────────────────────

async def take_screenshots(html_path: Path):
    from playwright.async_api import async_playwright

    # Patch the HTML so ALL sections are display:block before any JS runs.
    # Charts inside display:none measure 0-width → broken axis layouts.
    # We keep all sections visible throughout and clip each screenshot to
    # the relevant section's bounding box — no show/hide toggling at all.
    patched_path = html_path.parent / (html_path.stem + '_screenshot.html')
    src = html_path.read_text(encoding='utf-8')
    src = src.replace(
        '</head>',
        '<style>.section{display:block!important}</style></head>',
        1,
    )
    # Also pre-mark all sections active so nav highlights look right per screenshot
    patched_path.write_text(src, encoding='utf-8')

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={'width': 1400, 'height': 900},
            device_scale_factor=2,
        )

        await page.goto(f'file://{patched_path}', wait_until='networkidle')
        await page.wait_for_timeout(500)

        # Trigger every section's lazy init — all visible, correct widths.
        await page.evaluate('''
            Object.keys(sectionInits).forEach(sec => {
                if (!_init[sec]) { _init[sec] = true; sectionInits[sec](); }
            });
        ''')

        # Scroll full page slowly so every chart renders and animates.
        scroll_y = 0
        total_h = await page.evaluate('document.documentElement.scrollHeight')
        while scroll_y < total_h:
            scroll_y = min(scroll_y + 600, total_h)
            await page.evaluate(f'window.scrollTo(0, {scroll_y})')
            await page.wait_for_timeout(120)
            total_h = await page.evaluate('document.documentElement.scrollHeight')
        await page.evaluate('window.scrollTo(0, 0)')
        await page.wait_for_timeout(2000)

        for sec_name, filename in SECTIONS:
            print(f'  {sec_name}...')

            # Highlight the correct nav tab
            await page.evaluate(f'''
                document.querySelectorAll("nav a").forEach(a =>
                    a.classList.toggle("active", a.dataset.sec === "{sec_name}"));
            ''')
            await page.wait_for_timeout(200)

            # Build a composite: screenshot the sticky top bar + the section element.
            # We render the top (header+nav) and section separately then paste them.
            top_el = await page.query_selector('header')
            nav_el = await page.query_selector('nav')
            sec_el = await page.query_selector(f'#sec-{sec_name}')

            top_img_bytes  = await top_el.screenshot()
            nav_img_bytes  = await nav_el.screenshot()
            sec_img_bytes  = await sec_el.screenshot()

            from PIL import Image
            import io
            top_img = Image.open(io.BytesIO(top_img_bytes))
            nav_img = Image.open(io.BytesIO(nav_img_bytes))
            sec_img = Image.open(io.BytesIO(sec_img_bytes))

            total_h = top_img.height + nav_img.height + sec_img.height
            composite = Image.new('RGB', (sec_img.width, total_h), (13, 17, 23))
            composite.paste(top_img, (0, 0))
            composite.paste(nav_img, (0, top_img.height))
            composite.paste(sec_img, (0, top_img.height + nav_img.height))

            out = SCRIPT_DIR / filename
            composite.save(str(out))
            print(f'    → {out}')

        await browser.close()
        patched_path.unlink(missing_ok=True)

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 2:
        print('Usage: python3 docs/screenshot.py <path-to-git-repo>', file=sys.stderr)
        sys.exit(1)
    repo_path = sys.argv[1]

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: generate raw report
    print(f'Generating raw report from: {repo_path}')
    subprocess.run(
        ['python3', str(REPO_ROOT / 'gitstats.py'), repo_path, str(TMP_DIR)],
        check=True,
    )

    raw_html = TMP_DIR / 'index.html'
    obf_html = TMP_DIR / 'index_obf.html'
    sample_out = SCRIPT_DIR / 'sample_report.html'

    # Step 2: obfuscate
    print('Obfuscating...')
    html = raw_html.read_text(encoding='utf-8')
    obfuscated = obfuscate(html)
    obf_html.write_text(obfuscated, encoding='utf-8')
    sample_out.write_text(obfuscated, encoding='utf-8')
    print(f'  → {sample_out}')

    # Step 3: screenshots
    print('Taking screenshots...')
    await take_screenshots(obf_html)

    print('Done.')

if __name__ == '__main__':
    asyncio.run(main())
