<!-- AGENTS.md — 告诉 Agent 在这个仓库里怎么工作，不复制 README -->

# read-x · Agent 工作规则

## 项目定位

阅读系统：微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

README.md 保存项目事实；本文件保存 Agent 执行规则。

## 核心规则：群消息自动分派

**收到任何 `mp.weixin.qq.com` 链接时，必须走 long-read 全流程，禁止只给摘要。**

这是最高优先级规则。不要判断文章质量、不要问用户要不要精读、不要在群内直接输出摘要就结束。公众号文章进来 → long-read 全流程。

## long-read 全流程（硬性要求）

按 `.agents/skills/long-read/SKILL.md` 执行，不跳过任何步骤：

1. **抓取正文**：`scripts/wx_fast.py <url>`（httpx 直连优先，反爬回退 Camoufox）
2. **文体识别**：判断是否专项文体（访谈 Q&A、周刊等），是则走专项规则
3. **三段式精读摘要**：评分 → 一句话 → 骨架 → 值得记住
4. **ljg 深度链路**：根据内容自动选择 1-3 条
5. **输出**：含 ljg 产出时必须生成飞书文档 → 回群发完成卡片

## 关键目录

- `.agents/skills/long-read/` — long-read Skill 定义
- `scripts/wx_fast.py` — 微信文章快速抓取
- `scripts/wx_fetch.py` — 微信文章抓取（备用）
- `output/` — 已生成文档
- `outputs/` — 历史输出

## 常用命令

```bash
# 抓取微信文章
python3 scripts/wx_fast.py "<mp.weixin.qq.com URL>"

# 创建飞书文档（long-read 输出用）
lark-cli docs +create --title "<标题>" --content @.wx_doc.md --doc-format markdown --parent-position my_library

# 回群发完成消息
lark-cli im +messages-send --chat-id <chat_id> --markdown "..."
```

## 输出路由（硬性）

- 纯摘要无 ljg 且 ≤15 行 → 群内直接回复
- 含 ljg 产出或 >15 行 → **必须生成飞书文档 → 立即回群发完成卡片**
- 创建飞书文档后必须在同一轮自动回群，绝不等待用户催促

## 安全边界

- 不读取 `.env`、密钥、token
- 飞书文档创建走当前 bridge profile 的 lark-cli
- 临时文件（`.wx_tmp.md`、`.wx_doc.md`）用后清理

## 禁止事项

- 禁止对公众号链接只给摘要不生成文档
- 禁止跳过 long-read 流程中的任何步骤
- 禁止生成飞书文档后不回群通知
- 禁止对长文链接走普通 read skill 的默认摘要模式

## 验证方式

收到公众号链接后，确认：
- [ ] 正文已成功抓取
- [ ] 文体识别完成
- [ ] 三段式精读摘要已生成（含评分）
- [ ] ljg 链路已选择并执行
- [ ] 飞书文档已创建
- [ ] 回群完成卡片已发送
