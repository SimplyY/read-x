# Prompt 仓库备份

飞书是正文维护真源；本仓是公开 GitHub 仓库，用户已明确决定在此保留 Prompt 备份，推送后内容公开可见。备份供云环境读取，不作为运行时回退或缓存。备份由中央 `prompt-governance` 统一同步，项目仓库不实现同步脚本。

飞书人工编辑并保存后，运行中央命令；机器经发布入口写入并读回成功后会自动同步：

```bash
node /Users/yuwei/code/skills/prompt-governance/scripts/sync-backups.mjs --prompt-id <prompt_id> --confirm
```

本仓清单：`prompt-backups.json`。全局位置索引：`skills/prompt-governance/references/prompt-backups.md`。

| Prompt ID | 本仓路径 |
| --- | --- |
| `read-x.munger-analysis` | `prompts/read-x.munger-analysis.md` |
| `read-x.core-image` | `prompts/read-x.core-image.md` |
