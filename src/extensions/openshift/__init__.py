"""OpenShift extension (phase 2d Task 6).

detect() checks for the config.openshift.io API group, which is only present
on OpenShift clusters.  When mode='on' (konflux profile), the activation
runner skips detection entirely and calls register() directly.

register() installs all 3 OpenShift tools via ToolRegistry.register_server_tool,
which picks up the raw function bodies that live in server-mcp.py (decorators
were stripped in Task 6) and wraps them with execution logging.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.extension import DetectContext, ToolRegistry

TOOLS = (
    "get_etcd_logs",
    "get_machine_config_pool_status",
    "get_openshift_cluster_operator_status",
)  # name-sorted


class _OpenShiftExtension:
    name = "openshift"

    async def detect(self, ctx: "DetectContext") -> bool:
        return "config.openshift.io" in await ctx.discover_api_groups(ctx.instance)

    def register(self, reg: "ToolRegistry") -> None:
        for n in TOOLS:
            reg.register_server_tool(n)


EXTENSION = _OpenShiftExtension()
