import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.config import ResolvedConfig, SourceConfig, load_config
from core.profiles import BUILTIN_PROFILES


def test_absent_config_yields_builtin_konflux_profile():
    cfg = load_config(path=None, env={})
    assert cfg is BUILTIN_PROFILES["konflux"]
    assert cfg.profile == "konflux"


def test_builtin_konflux_sources_match_design():
    cfg = BUILTIN_PROFILES["konflux"]
    assert cfg.sources["kubernetes"] == SourceConfig(adapter="kubernetes", enabled=True, options={})
    assert cfg.sources["prometheus"] == SourceConfig(adapter="prometheus", enabled=True, options={})
    assert cfg.sources["kubearchive"] == SourceConfig(adapter="kubearchive", enabled=False, options={})
    assert cfg.extensions == {"konflux": "on", "openshift": "on", "tekton": "on"}


def test_builtin_kubernetes_and_standalone_profiles_exist():
    assert BUILTIN_PROFILES["kubernetes"].profile == "kubernetes"
    assert "kubearchive" not in BUILTIN_PROFILES["kubernetes"].sources
    assert BUILTIN_PROFILES["standalone"].sources == {}


def test_lumino_profile_env_selects_builtin():
    cfg = load_config(path=None, env={"LUMINO_PROFILE": "kubernetes"})
    assert cfg is BUILTIN_PROFILES["kubernetes"]


def test_unknown_profile_raises():
    with pytest.raises(ValueError, match="nope"):
        load_config(path=None, env={"LUMINO_PROFILE": "nope"})


def test_yaml_config_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "profile: custom\n"
        "sources:\n"
        "  loki-prod: {adapter: loki, enabled: true}\n"
        "extensions: {tekton: off}\n")
    cfg = load_config(path=str(p), env={})
    assert cfg.profile == "custom"
    assert cfg.sources["loki-prod"].adapter == "loki"
    assert cfg.extensions["tekton"] == "off"


def test_yaml_unknown_top_level_key_raises(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("profile: x\nbogus_key: 1\n")
    with pytest.raises(ValueError, match="bogus_key"):
        load_config(path=str(p), env={})


def test_lumino_config_env_points_at_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("profile: fromenv\n")
    cfg = load_config(path=None, env={"LUMINO_CONFIG": str(p)})
    assert cfg.profile == "fromenv"


# ── Extension validation (Task 2: banana fix) ───────────────────────────────────

def test_unknown_extension_name_raises(tmp_path):
    """Unknown extension names are rejected with clear error."""
    p = tmp_path / "config.yaml"
    p.write_text("extensions: {banana: on}\n")
    with pytest.raises(ValueError, match="unknown extension") as exc_info:
        load_config(path=str(p), env={})
    err = str(exc_info.value)
    assert "banana" in err
    # Error message must list known extensions
    assert "konflux" in err
    assert "openshift" in err
    assert "tekton" in err


def test_invalid_extension_mode_raises(tmp_path):
    """Invalid extension modes are rejected with clear error."""
    p = tmp_path / "config.yaml"
    p.write_text("extensions: {tekton: banana}\n")
    with pytest.raises(ValueError, match="invalid mode") as exc_info:
        load_config(path=str(p), env={})
    err = str(exc_info.value)
    assert "banana" in err
    assert "tekton" in err
    # Error message must list allowed modes
    assert "auto" in err
    assert "off" in err
    assert "on" in err


def test_yaml_bool_on_bare_normalizes_to_string(tmp_path):
    """Regression: YAML 1.1 bare 'on' parses as bool and normalizes to 'on' string."""
    p = tmp_path / "config.yaml"
    # Bare 'on' is parsed as boolean True by YAML 1.1; must normalize back to "on" string
    p.write_text("extensions: {tekton: on}\n")
    cfg = load_config(path=str(p), env={})
    assert cfg.extensions["tekton"] == "on"


def test_all_builtin_profiles_load_unchanged(tmp_path):
    """Regression: all builtin profiles pass validation (were valid before, stay valid)."""
    for profile_name, profile in BUILTIN_PROFILES.items():
        # Each profile's extensions must be valid
        for ext_name, mode in profile.extensions.items():
            assert ext_name in ("konflux", "openshift", "tekton"), \
                f"{profile_name} has unknown extension {ext_name!r}"
            assert mode in ("auto", "off", "on"), \
                f"{profile_name} has invalid mode {mode!r} for {ext_name}"
