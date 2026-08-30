"""Example workspace skill — loaded on boot and after workspace_write."""

NAME = "vault_echo"
DESCRIPTION = "Echo a short phrase. Use this to verify workspace skill loading on VAULT."
PARAMETERS = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Phrase to echo back."}
    },
    "required": ["text"],
}
DESTRUCTIVE = False


def run(args: dict, ctx: dict) -> dict:
    return {
        "echo": args.get("text", ""),
        "workspace": ctx.get("workspace"),
    }
