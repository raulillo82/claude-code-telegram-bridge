import os

from bridge.projects import list_projects, resolve_project


def make_projects(tmp_path, names):
    for name in names:
        (tmp_path / name).mkdir()
    return str(tmp_path)


def test_list_projects_ignores_hidden_and_files(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "some_file.txt").write_text("x")

    assert list_projects(str(tmp_path)) == ["alpha", "beta"]


def test_resolve_exact_match(tmp_path):
    d = make_projects(tmp_path, ["flight_vpn_tunnel", "nutri"])
    assert resolve_project("flight_vpn_tunnel", d) == ("flight_vpn_tunnel", "exact")


def test_resolve_case_insensitive(tmp_path):
    d = make_projects(tmp_path, ["Nutri"])
    assert resolve_project("nutri", d) == ("Nutri", "case-insensitive")


def test_resolve_unique_prefix(tmp_path):
    d = make_projects(tmp_path, ["wl-analysis-training", "nutri"])
    assert resolve_project("wl", d) == ("wl-analysis-training", "prefix")


def test_resolve_unique_substring(tmp_path):
    d = make_projects(tmp_path, ["wl-analysis-training", "nutri"])
    assert resolve_project("analysis", d) == ("wl-analysis-training", "substring")


def test_resolve_ambiguous_returns_candidates(tmp_path):
    d = make_projects(tmp_path, ["wl-analysis-training", "wl-old-experiment"])
    name, candidates = resolve_project("wl", d)
    assert name is None
    assert set(candidates) == {"wl-analysis-training", "wl-old-experiment"}


def test_resolve_no_match_returns_empty_list(tmp_path):
    d = make_projects(tmp_path, ["nutri"])
    name, candidates = resolve_project("asdfqwerty_no_existe", d)
    assert name is None
    assert candidates == []
