---
name: course-report
description: 为大学课程论文、学习心得、读书报告和实验报告生成结构清晰的 Markdown 草稿。适用于用户提供课程主题、作业要求、字数、参考材料或目标文件路径，并希望在工作区中产出可提交的报告草稿。
runtime: prompt-only
---

# Course Report

Produce an evidence-aware course report draft and save it only through Workspace tools.

## Workflow

1. Extract the report type, course, topic, audience, word count, required sections, references, and target path. State any necessary assumptions.
2. Read only the relevant workspace materials. Distinguish source facts from your own synthesis; never invent citations, quotations, data, experiments, or reading notes.
3. Use [assets/report-template.md](assets/report-template.md) as a flexible structure. Adapt headings to the assignment instead of filling sections mechanically.
4. Draft a coherent argument with a clear introduction, logically ordered body, and conclusion. Match the requested length and register.
5. Check the draft against [references/checklist.md](references/checklist.md).
6. If the user supplied a save path, use `create_file` for a new Markdown draft. When revising an existing long report, read the latest relevant text and prefer `apply_patch` for focused changes instead of resending the whole file through `overwrite_file`. Use `overwrite_file` only when the user explicitly needs a complete replacement. These tools require user approval; do not claim the file was saved before the Tool Result confirms success.
7. Report the output path, important assumptions, and any missing evidence the user should verify.

Do not place temporary task state in memory or modify files without the existing approval flow.
