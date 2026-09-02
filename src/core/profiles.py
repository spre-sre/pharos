"""Built-in profile definitions (spec SS4.6).  Pure data — importing this
module performs no I/O."""
from __future__ import annotations

from core.config_types import ResolvedConfig, SourceConfig

BUILTIN_PROFILES = {
    "konflux": ResolvedConfig(
        profile="konflux",
        sources={
            "kubernetes": SourceConfig(adapter="kubernetes", enabled=True, options={}),
            "prometheus": SourceConfig(adapter="prometheus", enabled=True, options={}),
            "kubearchive": SourceConfig(adapter="kubearchive", enabled=False, options={}),
        },
        extensions={"konflux": "on", "openshift": "on", "tekton": "on"},
    ),
    "kubernetes": ResolvedConfig(
        profile="kubernetes",
        sources={
            "kubernetes": SourceConfig(adapter="kubernetes", enabled=True, options={}),
            "prometheus": SourceConfig(adapter="prometheus", enabled=True, options={}),
        },
        extensions={"tekton": "off", "openshift": "off", "konflux": "off"},
    ),
    "standalone": ResolvedConfig(profile="standalone", sources={}, extensions={}),
}
