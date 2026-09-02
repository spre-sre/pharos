"""Tekton extension (phase 2d Task 6).

detect() checks for the tekton.dev API group, which is only present on
clusters that have Tekton Pipelines installed.  When mode='on' (konflux
profile), the activation runner skips detection entirely and calls
register() directly.

register() installs all 7 Tekton tools via ToolRegistry.register_server_tool,
which picks up the raw function bodies that live in server-mcp.py (decorators
were stripped in Task 6) and wraps them with execution logging.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.extension import DetectContext, ToolRegistry

TOOLS = (
    "analyze_failed_pipeline",
    "find_pipeline",
    "get_pipelinerun_logs",
    "get_tekton_pipeline_runs_status",
    "list_pipelineruns",
    "list_recent_pipeline_runs",
    "list_taskruns",
)  # name-sorted


class _TektonExtension:
    name = "tekton"

    async def detect(self, ctx: "DetectContext") -> bool:
        return "tekton.dev" in await ctx.discover_api_groups(ctx.instance)

    def register(self, reg: "ToolRegistry") -> None:
        for n in TOOLS:
            reg.register_server_tool(n)


EXTENSION = _TektonExtension()
