# 非微信 URL 抓取（即刻 / 其他）

> 本文件自 link-card/SKILL.md 移入，原文未改动；入口保留触发条件与索引。

其他 URL 使用 `read` skill 抓取。准备细节与即刻抓取命令如下：

非微信来源才先执行一次 `mktemp -d /tmp/readx-score.XXXXXX`，抓取后调用 `prepare_anchor_view.py --blind-only --article-source <source.md> --blind-output <blind-source.md>`，再运行 `python3 /Users/yuwei/code/read-x/scripts/fetch_base_config.py --output <run_dir>/base-config.json`。本次所有产物写入该目录；禁止固定共享路径或写入仓库。Base 读取失败时继续使用本地策略，并保留失败原因。

即刻抓取命令：
```bash
curl -s -L -H "User-Agent: Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)" \
  "<url>" | python3 -c "
import sys, json, re
html = sys.stdin.read()
m = re.search(r'<script id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>', html, re.DOTALL)
if m:
    d = json.loads(m.group(1))
    post = d['props']['pageProps']['post']
    print(json.dumps({'content':post.get('content',''),'author':post.get('author',''),'time':post.get('createdAt',''),'title':post.get('title',''),'likeCount':post.get('likeCount',0)}, ensure_ascii=False))
else:
    print('{"error":"NEXT_DATA not found"}')
"
```
