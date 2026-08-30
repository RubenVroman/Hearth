# Workspace

This directory is the only filesystem Hearth can read or write.

Drop Python files in `skills/` to add tools at runtime. Each skill module must define:

```python
NAME = "my_skill"
DESCRIPTION = "What it does."
PARAMETERS = {
    "type": "object",
    "properties": {"example": {"type": "string"}},
    "required": ["example"],
}
DESTRUCTIVE = False

def run(args: dict, ctx: dict) -> dict:
    return {"ok": True}
```

The agent can write new skills here (`workspace_write` runs immediately in the sandbox). Paths cannot leave this tree — the rest of the NAS is out of reach.
