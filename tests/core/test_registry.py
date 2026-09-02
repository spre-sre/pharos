import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.config_types import ResolvedConfig, SourceConfig
from core.profiles import BUILTIN_PROFILES
from core.registry import ADAPTER_CAPABILITIES, AdapterRegistry, build_registry


def test_build_from_konflux_profile_skips_disabled():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    names = [e.name for e in reg.entries()]
    assert names == ["kubernetes", "prometheus"]  # kubearchive disabled; sorted


def test_entries_are_name_sorted_regardless_of_config_order():
    cfg = ResolvedConfig(profile="x", sources={
        "zeta": SourceConfig(adapter="prometheus"),
        "alpha": SourceConfig(adapter="kubernetes"),
    })
    assert [e.name for e in build_registry(cfg).entries()] == ["alpha", "zeta"]


def test_capabilities_come_from_adapter_type():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    assert reg.get("kubernetes").capabilities == ("Log", "Event", "Inventory")
    assert reg.get("prometheus").capabilities == ("Metric",)


def test_capable_of():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    assert reg.capable_of("Log") == ["kubernetes"]
    assert reg.capable_of("Metric") == ["prometheus"]
    assert reg.capable_of("Inventory") == ["kubernetes"]


def test_default_kubernetes_instance():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    assert reg.default_kubernetes_instance() == "kubernetes"
    assert build_registry(BUILTIN_PROFILES["standalone"]).default_kubernetes_instance() is None


def test_get_unknown_raises_with_known_names():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    with pytest.raises(KeyError, match="list_sources"):
        reg.get("nope")


def test_unknown_adapter_type_raises():
    cfg = ResolvedConfig(profile="x", sources={"s": SourceConfig(adapter="bogus")})
    with pytest.raises(ValueError, match="bogus"):
        build_registry(cfg)


def test_state_is_configured_in_2a():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    assert {e.state for e in reg.entries()} == {"configured"}


def test_default_instance_of():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    assert reg.default_instance_of("kubernetes") == "kubernetes"
    assert reg.default_instance_of("prometheus") == "prometheus"
    assert reg.default_instance_of("loki") is None


def test_default_kubernetes_instance_delegates():
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    assert reg.default_kubernetes_instance() == reg.default_instance_of("kubernetes")


# ─── ES capability trim (phase-4 scope cut) ───────────────────────────────────
# Task 4: ADAPTER_CAPABILITIES["elasticsearch"] is TRIMMED to ("Log",) only.
# Event/Metric are deferred to phase 4b (# TODO(4b): restore Event/Metric as they ship).
# F6 from the plan: no existing assertion covers the ES tuple — this is the NEW
# positive lock test that pins the phase-4 scope cut.


def test_elasticsearch_capabilities_trimmed_to_log_only():
    """elasticsearch adapter is Log-only in phase 4 (Event/Metric deferred to 4b).

    Build a registry with a single elasticsearch source and verify that the
    resolved capabilities tuple is exactly ("Log",) — not the pre-trim
    ("Log", "Event", "Metric") value.

    This is the capability-trim lock test (F6): it prevents phantom additions
    (e.g. re-adding Event before phase 4b ships) from silently routing through
    unimplemented protocols.
    """
    cfg = ResolvedConfig(
        profile="test",
        sources={"my-es": SourceConfig(adapter="elasticsearch")},
    )
    reg = build_registry(cfg)
    assert reg.get("my-es").capabilities == ("Log",), (
        "elasticsearch capabilities must be ('Log',) in phase 4; "
        "Event/Metric deferred to TODO(4b) — check ADAPTER_CAPABILITIES in registry.py"
    )


# ─── Phase 5 Task 2: OTLP capability trim lock test ──────────────────────────
# ADAPTER_CAPABILITIES["otlp"] is TRIMMED to ("Log",) only in phase 5.
# Event/Metric are deferred to phase 5b (# TODO(5b): restore Event/Metric as they ship).
# Lock test: pins the phase-5 scope cut; prevents phantom additions.


def test_otlp_capabilities_trimmed_to_log_only():
    """otlp adapter is Log-only in phase 5 (Event/Metric deferred to 5b).

    Build a registry with a single otlp source and verify that the resolved
    capabilities tuple is exactly ("Log",) — not the pre-trim
    ("Log", "Event", "Metric") value.

    This is the capability-trim lock test (phase 5 mirror of elasticsearch F6):
    it prevents phantom additions (e.g. re-adding Event before phase 5b ships)
    from silently routing through unimplemented protocols.
    """
    cfg = ResolvedConfig(
        profile="test",
        sources={"my-otlp": SourceConfig(adapter="otlp")},
    )
    reg = build_registry(cfg)
    assert reg.get("my-otlp").capabilities == ("Log",), (
        "otlp capabilities must be ('Log',) in phase 5; "
        "Event/Metric deferred to TODO(5b) — check ADAPTER_CAPABILITIES in registry.py"
    )


# ─── Phase 5 Task 2: F11 lock test — no builtin profile declares an otlp source ─

def test_builtin_profiles_standalone_has_empty_sources():
    """BUILTIN_PROFILES['standalone'].sources == {} (F11: no auto-on sources).

    The standalone profile is the zero-source baseline; verifying it stays
    empty prevents accidental auto-registration of the OTLP adapter.
    """
    from core.profiles import BUILTIN_PROFILES
    assert BUILTIN_PROFILES["standalone"].sources == {}, (
        "standalone profile must have no sources (F11 lock); "
        "otlp is config-declared only, never auto-on"
    )


def test_builtin_profiles_have_no_otlp_source():
    """No builtin profile declares an otlp source (F11: config-declared only).

    OTLP is intentionally absent from all builtin profiles because the
    receiver needs explicit token and capacity configuration — starting an
    OTLP listener by default would violate the fail-closed requirement (M2).
    """
    from core.profiles import BUILTIN_PROFILES
    for profile_name, cfg in BUILTIN_PROFILES.items():
        for src_name, sc in cfg.sources.items():
            assert sc.adapter != "otlp", (
                f"builtin profile {profile_name!r} declares an otlp source "
                f"{src_name!r}; otlp is config-declared only (F11 lock)"
            )


# ─── Phase 2e Task 1: add_instance + default marker ──────────────────────────

# (g) add_instance appends; duplicate name raises ValueError
def test_add_instance_appends():
    """add_instance adds a new entry to the registry."""
    from core.registry import SourceEntry
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    entry_before = [e.name for e in reg.entries()]
    assert "new-cluster" not in entry_before

    new = SourceEntry(
        name="new-cluster",
        adapter="kubernetes",
        capabilities=ADAPTER_CAPABILITIES["kubernetes"],
        state="configured",
    )
    reg.add_instance(new)
    entry_after = [e.name for e in reg.entries()]
    assert "new-cluster" in entry_after


def test_add_instance_duplicate_raises():
    """add_instance raises ValueError when the name is already registered."""
    from core.registry import SourceEntry
    reg = build_registry(BUILTIN_PROFILES["konflux"])
    dup = SourceEntry(
        name="kubernetes",  # already in konflux profile
        adapter="kubernetes",
        capabilities=ADAPTER_CAPABILITIES["kubernetes"],
        state="discovered",
    )
    with pytest.raises(ValueError, match="kubernetes"):
        reg.add_instance(dup)


# (h) R1 mutation pin: explicit default=True beats alphabetical order
def test_default_instance_of_uses_explicit_flag():
    """default_instance_of returns the entry with default=True, even when
    another entry would sort first alphabetically.

    Registry: aaa-cluster (no flag) + kubernetes (default=True).
    Sorted order would yield 'aaa-cluster', but the flag must win.
    """
    from core.registry import SourceEntry, AdapterRegistry
    entries = [
        SourceEntry(
            name="kubernetes",
            adapter="kubernetes",
            capabilities=ADAPTER_CAPABILITIES["kubernetes"],
            state="configured",
            default=True,
        ),
        SourceEntry(
            name="aaa-cluster",
            adapter="kubernetes",
            capabilities=ADAPTER_CAPABILITIES["kubernetes"],
            state="configured",
            default=False,
        ),
    ]
    reg = AdapterRegistry(entries)
    assert reg.default_instance_of("kubernetes") == "kubernetes", (
        "default_instance_of must return the entry marked default=True "
        "('kubernetes'), not the alphabetically-first entry ('aaa-cluster')"
    )


# (i) no-default fallback: sorted()[0] when no entry carries default=True
def test_default_instance_of_falls_back_to_sorted_when_no_flag():
    """When no entry has default=True, fall back to sorted()[0] (back-compat)."""
    from core.registry import SourceEntry, AdapterRegistry
    entries = [
        SourceEntry(
            name="zzz-cluster",
            adapter="kubernetes",
            capabilities=ADAPTER_CAPABILITIES["kubernetes"],
            state="configured",
            default=False,
        ),
        SourceEntry(
            name="aaa-cluster",
            adapter="kubernetes",
            capabilities=ADAPTER_CAPABILITIES["kubernetes"],
            state="configured",
            default=False,
        ),
    ]
    reg = AdapterRegistry(entries)
    result = reg.default_instance_of("kubernetes")
    assert result == "aaa-cluster", (
        f"Without any default=True flag, sorted()[0]='aaa-cluster' should be "
        f"returned, got {result!r}"
    )


# (F5) build_registry invariant: exactly one kubernetes entry marked default=True
def test_build_registry_marks_sorted_first_kubernetes_as_default():
    """build_registry marks exactly ONE kubernetes entry default=True — the
    sorted()[0] of config-declared kubernetes sources (round-1 F5).

    This test uses a synthetic config whose kubernetes source is named
    'prod-cluster', then adds a runtime-discovered instance 'aaa-ctx'.
    The default must remain 'prod-cluster' (build-time anchor), not 'aaa-ctx'
    (which sorts first alphabetically) — the sort-order hijack is dead.
    """
    from core.registry import SourceEntry
    cfg = ResolvedConfig(
        profile="test",
        sources={"prod-cluster": SourceConfig(adapter="kubernetes")},
    )
    reg = build_registry(cfg)

    # Confirm build_registry marks the sole kubernetes entry as default
    entry = reg.get("prod-cluster")
    assert entry.default is True, (
        "build_registry must mark the sorted()[0] kubernetes entry with default=True"
    )

    # Now add a discovered instance that sorts before "prod-cluster"
    discovered = SourceEntry(
        name="aaa-ctx",
        adapter="kubernetes",
        capabilities=ADAPTER_CAPABILITIES["kubernetes"],
        state="discovered",
        default=False,  # NOT the build-time default
    )
    reg.add_instance(discovered)

    # The default must still be "prod-cluster" (explicit flag wins over alpha order)
    assert reg.default_instance_of("kubernetes") == "prod-cluster", (
        "After adding runtime instance 'aaa-ctx' (sorts before 'prod-cluster'), "
        "default_instance_of must still return 'prod-cluster' — the build-time anchor"
    )
