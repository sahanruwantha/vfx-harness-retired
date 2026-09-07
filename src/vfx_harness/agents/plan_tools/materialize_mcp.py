"""Current Claude transport for shared VFX materialization operations.

Removed when the remaining JIT model session switches to Flynn. Domain handlers and
schemas live outside this transport and have no dependency on either model SDK.
"""

from claude_agent_sdk import tool

from vfx_harness.agents.materialization_operations import materialization_operations


def _adapt(operation):
    async def handler(arguments):
        result = await operation.handler(arguments)
        return {"content": [{"type": "text", "text": result.text}],
                **({"is_error": True} if result.refused else {})}

    return tool(operation.name, operation.description, operation.input_schema)(handler)


def register_materialize_tools(**closed):
    return tuple(_adapt(operation) for operation in materialization_operations(**closed))
