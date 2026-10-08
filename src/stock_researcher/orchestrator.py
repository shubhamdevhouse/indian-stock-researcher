"""Builds the Claude Agent SDK options for the orchestrator + subagents."""
from claude_agent_sdk import ClaudeAgentOptions

from . import config
from .agents import ORCHESTRATOR_PROMPT, subagents
from .tools import ALL_TOOLS, SERVER_KEY, build_server, tool_name


def build_options() -> ClaudeAgentOptions:
    market_tools = [tool_name(t) for t in ALL_TOOLS]
    return ClaudeAgentOptions(
        system_prompt=ORCHESTRATOR_PROMPT,
        model=config.MAIN_MODEL,
        mcp_servers={SERVER_KEY: build_server()},
        agents=subagents(),
        tools=["Agent"],  # only built-in tool exposed: subagent delegation
        allowed_tools=market_tools + ["Agent", "Task"],
        disallowed_tools=["Bash", "Write", "Edit", "Read", "WebSearch", "WebFetch", "NotebookEdit"],
        setting_sources=[],
        max_turns=config.MAX_TURNS,
        cwd=str(config.PROJECT_ROOT),
        # tools may wait on the user (count picker) or run long batched downloads (cold NIFTY 200 ≈ 9 min)
        env={"MCP_TOOL_TIMEOUT": str(3600 * 1000)},
    )
