# read-x

阅读系统：微信公众号长文精读、结构化拆解、飞书文档输出。同时管理微信读书。

## 项目事实

- **仓库**：`https://github.com/SimplyY/read-x`（私有）
- **飞书群**：read-x（`oc_f79c12bcb643eb26c486282f933333a0`）
- **工作目录**：`/Users/yuwei/code/read-x`
- **默认机器人**：Codex / Code X bot

## 目录结构

```
read-x/
├── .agents/skills/long-read/  # long-read Skill 定义
├── scripts/                    # 微信文章抓取脚本（备用）
├── output/                     # 已生成文档
├── outputs/                    # ljg 深度链路产物
├── AGENTS.md                   # Agent 执行规则
├── GROUP_INFO.md               # 群绑定信息
└── README.md                   # 本文件
```

## 依赖

- `wechat-article-to-markdown` skill — 微信文章抓取
- `lark-cli`（bridge profile 注入）— 飞书文档创建、群消息发送
- ljg 系列 skill — 深度链路
