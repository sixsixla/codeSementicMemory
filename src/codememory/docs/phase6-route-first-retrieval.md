# Phase 6A：Route-first 检索

## 目标

CodeMemory 的默认用途是给 Coding Agent 提供高召回的历史代码入口，而不是
在写入前把每条记忆证明成不可质疑的事实。候选记忆可以先作为路由提示返回，
再由 Agent 结合当前源码验证。

## 检索模式

### `route`（默认）

- 隐藏 `quarantine`、`rejected`、`superseded`；
- 保留 `proposed`、`uncertain`、质量为 `review` 的卡片；
- `stable` 和 `verified` 排在候选提示之前；
- 返回 `route.trust_level`、`route.score`、`entrypoints`、绑定状态和质量决策。

### `trusted`

- 只返回 `verified`/`stable` 卡片；
- 要求至少一个质量为 `accepted` 的来源候选；
- 有 `review` 或 `quarantine` 来源的卡片不返回。

### `audit`

- 用于检查完整卡片生命周期；
- 配合 `include_quarantine` 才会查看 quarantine 支撑的卡片。

## 路由评分

评分是可解释的确定性排序，不替代代码验证：

```text
status level       40%
quality decision   22%
binding state      18%
card confidence    12%
text/alias match    8%
```

多词查询会先尝试 token-OR 扩展，避免自然语言请求必须完整匹配同一张卡片。

## Agent 使用约束

`route` 结果是入口假设，不是编辑授权。Agent 应检查当前文件、符号、调用链和
测试结果；任务结束时通过 capture/finish 写回实际使用和验证结果。成功重复使用
可以推动卡片从 `route_hint` 晋升到 `verified_route` 或 `stable_knowledge`，失败或
重构则应降权、标记 stale 或形成 anti-binding。
