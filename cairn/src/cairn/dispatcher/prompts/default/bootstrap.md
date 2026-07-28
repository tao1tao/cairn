# Task
You are doing initial reconnaissance on a target. Gather comprehensive information about the target. Run commands to discover: identity, infrastructure, technologies, subdomains, open ports, security mechanisms, and any other relevant information.

When you have gathered sufficient information, output your findings.

# Output Requirements
Return only one raw JSON object:

```json
{"accepted": true, "data": {"fact": {"description": "..."}, "complete": {"description": "..."}}}
```

# Rules
- Run reconnaissance commands: curl, host/dig, whatweb, nmap (quick scan), and similar tools
- Be thorough but focused. Gather what you can in a reasonable time
- `fact.description` must state the key findings clearly (identity, tech stack, infrastructure, etc.)
- `complete.description` should summarize what was discovered

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
