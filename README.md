# read-x

阅读系统：微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

## 项目事实

- **仓库**：`https://github.com/SimplyY/read-x`（私有）
- **飞书群**：read-x（`oc_f79c12bcb643eb26c486282f933333a0`）
- **工作目录**：`/Users/yuwei/code/read-x`
- **默认机器人**：Codex / Code X bot

## 核心能力

收到微信公众号链接（`mp.weixin.qq.com`）自动触发 long-read 全流程：

1. 用 `wechat-article-to-markdown` skill 抓取正文
2. 文体识别（访谈 Q&A、周刊等专项）
3. 三段式精读摘要（评分 → 一句话 → 骨架 → 值得记住）
4. 根据内容自动选择 ljg 深度链路（think/learn/roundtable/writes/qa/word）
5. 含 ljg 产出时生成飞书文档，回群发完成卡片

## 目录结构

```
read-x/
├── .agents/skills/long-read/  # long-read Skill 定义
├── scripts/                    # 微信文章抓取脚本（备用）
├── output/                     # 已生成文档
├── outputs/                    # 历史输出（ljg 链路产物）
├── AGENTS.md                   # Agent 执行规则
├── GROUP_INFO.md               # 群绑定信息
└── README.md                   # 本文件
```

## 依赖

- `wechat-article-to-markdown` skill — 微信文章抓取
- `lark-cli`（bridge profile 注入）— 飞书文档创建、群消息发送
- ljg 系列 skill — 深度链路

## 输出路由

- 纯摘要无 ljg 且 ≤15 行 → 群内直接回复
- 含 ljg 产出或 >15 行 → 生成飞书文档，回群发完成卡片
