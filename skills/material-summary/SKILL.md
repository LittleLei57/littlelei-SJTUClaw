---
name: material-summary
description: 汇总并整合工作区中的学习材料、课堂笔记、研究记录和多份文本文件。适用于提炼要点、对比观点、梳理证据、发现待解决问题，或根据已有材料生成可追溯的 Markdown 摘要。
runtime: prompt-only
---

# Material Summary

Build a traceable synthesis from workspace materials.

## Workflow

1. Confirm the material scope, desired depth, audience, output format, and optional save path.
2. Use `list_dir` and `read_file` to inspect only relevant files. If a file is truncated or unreadable, disclose it.
3. Separate direct source content, cross-source synthesis, and your own inference.
4. Organize the result as: scope, executive summary, key points, agreements or conflicts, evidence by source, unresolved questions, and suggested next steps.
5. Apply [references/checklist.md](references/checklist.md) before returning the result.
6. Save only when requested, using an approved Workspace Update Tool. Never imply a save succeeded without a successful tool result.

Do not fabricate details missing from the materials or silently treat one source as representative of all sources.
