# Turn、审批与取消生命周期

本页说明 SJTUClaw 在 Web、CLI、Scheduler 和外部渠道共用的 Agent Turn 状态机。实现入口主要位于 `runtime.py`、`gateway.py`、`turn_store.py` 与 `approval_store.py`。

## 生命周期

```text
starting -> running -> completed
                  \-> awaiting_approval -> running (同一 turnId，新 runId)
                  \-> cancelling -> cancelled
                  \-> error / interrupted
```

- `turnId` 标识一次用户请求，审批暂停与恢复始终属于同一个 Turn。
- `runId` 是一次执行租约。恢复、重试会取得新租约；旧 worker 的晚到事件会被 fencing 拒绝。
- `seq` 是 Turn 内单调递增事件序号，刷新重放和 SSE 实时事件以它去重。
- 流式 `assistant_delta` 只实时广播，不逐字写 SQLite；最终正文和生命周期事件才持久化，避免高频写盘和刷新后的碎片重放。

## 核心不变量

1. **终态只提交一次**：先持久化 `completed/cancelled/error`，再发 `done`；晚到清理不能覆盖终态。
2. **取消先落盘**：取消请求先写 TurnStore，再通知内存 worker；即使刷新或 Gateway 重启，取消意图也不会丢失。
3. **审批恰好执行一次**：Approval 使用原子 claim；重复点击只返回既有决策，不能重复执行副作用 Tool。
4. **批准不等于成功**：Tool 成功、失败、取消和结果未知分别保存；失败重试生成新的 Approval lineage。
5. **完成必须有证据**：结构化 Planner 只能由成功 Tool Result 推进。缺少读取、写入、执行等能力证据时，即使模型说“完成了”，计划仍会被阻塞。
6. **失败不污染会话**：模型/协议异常会回滚本轮用户正文、半截工具协议与临时计划，只保留 Turn/activity 审计；等待审批属于可恢复暂停，不回滚。
7. **取消语义符合入口**：Web/渠道停止时保留已发送问题以便重答；CLI Ctrl+C 丢弃未完成输入并返回提示符。
8. **有界纠错**：协议修复、完成校验和重复 Tool 均有次数上限，避免“承诺但不调用”或无进展循环。

## 刷新、重启与审批恢复

- Web 刷新后先读取 TurnStore 快照，再订阅事件流；历史事件和实时事件按 `turnId + seq` 合并。
- Gateway 重启时，真正处于运行态的 Turn 标记为 `interrupted`；已经持久化的终态不会被误判。
- 等待审批的 Turn 可跨刷新恢复。批准后 ApprovalStore 原子占用执行权，Runtime 把 Tool Result 回灌同一 Session，再继续 Agent Loop。
- 页面关闭不会自动批准、拒绝或重复执行 Tool。

## Planner 与 Execution Evidence

复杂任务会建立“计划—执行—验证”步骤。Planner 只是可观察状态，不替代 Agent Loop：

```text
用户输入 -> buildContext -> callLLM
  -> final: 校验证据并结束
  -> tool_call(s): 执行/等待审批 -> 写入 Tool Result
  -> 重新 buildContext，继续循环
```

读取、搜索、写入、命令执行、Skill 安装和 Scheduler 修改等结论必须能追溯到真实 Tool Result。最终回答出现完成性声明但缺少证据时，Runtime 只进行有界修复；仍无证据就如实说明未完成。

## 回归测试

重点测试位于：

- `test/test_turn_store.py`：CAS、run lease、取消和终态 fencing；
- `test/test_workspace_tools.py`：审批并发 claim、恢复、失败 lineage 与取消；
- `test/test_streaming.py`：SSE 刷新重放、停止和同 Turn 恢复；
- `test/test_task_planner.py`：步骤只能由成功证据推进；
- `test/test_execution_evidence.py`：完成性声明与真实执行证据校验。
