#!/usr/bin/env python3
"""Fast WeChat article fetcher: curl + stdlib only, ~3s."""
import sys, re, html, urllib.request, argparse, os
from html.parser import HTMLParser

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0
        self.skip_tags = {'script','style','svg','noscript','iframe'}
        self.cur_tag = None
        self.in_bold = 0
        self.in_heading = 0
    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        self.cur_tag = t
        if t in self.skip_tags:
            self.skip_depth += 1
            return
        if t in ('strong','b'):
            if self.in_bold == 0:
                self.parts.append('**')
            self.in_bold += 1
        if re.match(r'h[1-6]', t):
            self.in_heading += 1
            self.parts.append('\n\n## ' if t in ('h1','h2') else '\n### ')
        if t == 'p' or t == 'br':
            self.parts.append('\n')
        if t == 'img':
            for k,v in attrs:
                if k == 'alt' and v:
                    self.parts.append(f'[{v}]')
        if t == 'a':
            href = dict(attrs).get('href','')
        if t == 'li':
            self.parts.append('\n- ')
        if t == 'blockquote':
            self.parts.append('\n> ')
    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.skip_tags:
            self.skip_depth = max(0, self.skip_depth-1)
            return
        if t in ('strong','b'):
            self.in_bold -= 1
            if self.in_bold == 0:
                self.parts.append('**')
        if re.match(r'h[1-6]', t):
            self.in_heading -= 1
            self.parts.append('\n')
        if t == 'p':
            self.parts.append('\n')
    def handle_data(self, data):
        if self.skip_depth > 0: return
        self.parts.append(data)
    def text(self):
        return ''.join(self.parts)

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode('utf-8', errors='replace')

def extract(h):
    def one(pat, default=''):
        m = re.search(pat, h, re.S)
        return html.unescape(m.group(1).strip()) if m else default
    # title (may contain inner span)
    title_m = re.search(r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>', h, re.S)
    title = re.sub(r'<[^>]+>','', title_m.group(1)) if title_m else ''
    title = html.unescape(title.strip())
    account = one(r'id="js_name"[^>]*>([^<]+)')
    pub_time = one(r'id="publish_time"[^>]*>([^<]+)') or one(r'var\s+ct\s*=\s*"(\d+)"', '')
    if pub_time.isdigit():
        import datetime
        pub_time = datetime.datetime.fromtimestamp(int(pub_time)).strftime('%Y-%m-%d %H:%M:%S')
    # rich_media_meta area for author/source
    author = one(r'class="rich_media_meta_nickname"[^>]*>.*?<a[^>]*>([^<]+)', '')
    # content div
    cm = re.search(r'id="js_content"[^>]*>(.*?)(?:</div>\s*<script|<script>\s*var\s+msg_cdn_url)', h, re.S)
    if not cm:
        # fallback: greedy
        cm = re.search(r'id="js_content"[^>]*>(.*)', h, re.S)
    content_html = cm.group(1) if cm else ''
    p = TextExtractor()
    p.feed(content_html)
    body = p.text()
    # cleanup
    body = re.sub(r'\n{3,}', '\n\n', body)
    body = re.sub(r'[ \t]+\n', '\n', body)
    body = re.sub(r'\*\*\s*\*\*', '', body)
    return {'title': title, 'account': account, 'time': pub_time, 'author': author, 'body': body}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('-o', '--output', default='-')
    args = ap.parse_args()
    h = fetch(args.url)
    d = extract(h)
    md = f"""# {d['title']}

> 公众号: {d['account']}
> 发布时间: {d['time']}
> 原文链接: {args.url}

---
{d['body']}
"""
    if args.output == '-':
        sys.stdout.write(md)
    else:
        with open(args.output,'w',encoding='utf-8') as f:
            f.write(md)
        print(f'Saved: {args.output} ({len(md)} chars)', file=sys.stderr)

if __name__ == '__main__':
    main()
