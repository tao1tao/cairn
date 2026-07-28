# 任务
你正在对目标进行初始侦察。收集关于目标的全面信息。运行命令以发现：身份、基础设施、技术栈、子域名、开放端口、安全机制以及任何其他相关信息。

当收集到足够的信息后，输出你的发现。

# 输出格式
只返回一个原始 JSON 对象：

```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

# 规则
- 运行侦察命令：curl、host/dig、whatweb、nmap（快速扫描）等工具
- 全面但有重点。在合理时间内尽可能多地收集信息
- `fact.description` 必须清晰陈述关键发现（身份、技术栈、基础设施等）
- **对发现进行分类**：在 `fact.description` 开头添加严重等级和标签，每行一个：
  ```
  [SEVERITY: Critical/High/Medium/Low/Info]
  [TARGET: target-system-or-endpoint]
  ```
  一般侦察用 "Info"。只有明确有安全影响时才使用 Critical/High。

# 上下文
## 起点
```
{origin}
```

## 目标
```
{goal}
```

## 提示
```
{hints}
```
