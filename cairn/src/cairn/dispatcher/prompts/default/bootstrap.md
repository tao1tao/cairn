# Task
You are doing initial reconnaissance on a target. Gather comprehensive information about the target. Run commands to discover: identity, infrastructure, technologies, subdomains, open ports, security mechanisms, and any other relevant information.

When you have gathered sufficient information, output your findings.

# Output Requirements
Return only one raw JSON object:

```json
{"accepted": true, "data": {"fact": {"description": "..."}}}
```

If you are confident the Goal is already satisfied based on what you found, you may also add a `complete` field:
```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

# Rules
- Run reconnaissance commands: curl, host/dig, whatweb, nmap (quick scan), and similar tools
- Be thorough but focused. Gather what you can in a reasonable time
- `fact.description` must state the key findings clearly (identity, tech stack, infrastructure, etc.)
- Only add `complete` if you are truly certain Goal is met — most initial reconnaissance cannot confirm Goal
- **Classify your finding**: At the beginning of `fact.description`, add severity and labels on separate lines when applicable:
  ```
  [SEVERITY: Critical/High/Medium/Low/Info]
  [TARGET: target-system-or-endpoint]
  ```
  Use "Info" for general reconnaissance. Only use Critical/High for findings with clear security impact.

# Context
## Origin
```
{origin}
```

## Goal
```
{goal}
```

## Hints
```
{hints}
```
