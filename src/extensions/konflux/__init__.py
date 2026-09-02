"""Konflux extension (phase 2d Task 5).

detect() checks for the appstudio.redhat.com API group, which is only
present on Konflux/AppStudio clusters.  When mode='on' (konflux profile),
the activation runner skips detection entirely and calls register() directly.

register() installs pipeline_tracer and ci_cd_performance_baselining_tool
via the ToolRegistry facade.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.extension import DetectContext, ToolRegistry


class _KonfluxExtension:
    name = "konflux"

    async def detect(self, ctx: "DetectContext") -> bool:
        return "appstudio.redhat.com" in await ctx.discover_api_groups(ctx.instance)

    def register(self, reg: "ToolRegistry") -> None:
        from .tools import make_ci_cd_performance_baselining_tool, make_pipeline_tracer

        for fn in (make_pipeline_tracer(reg), make_ci_cd_performance_baselining_tool(reg)):
            reg.tool()(fn)


EXTENSION = _KonfluxExtension()
