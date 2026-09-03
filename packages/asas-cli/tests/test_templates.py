from asas_cli.registry import PACKAGES
from asas_cli.templates import SNIPPETS, get


def test_every_registered_package_has_a_snippet():
    assert set(SNIPPETS) == set(PACKAGES)


def test_get_matches_dict_lookup():
    for key in SNIPPETS:
        assert get(key) is SNIPPETS[key]


def test_table_owning_packages_call_migrate_in_boot():
    table_owning = {
        "lookups",
        "jobs",
        "access",
        "workflow",
        "notifications",
        "search",
    }
    for key in table_owning:
        boot_text = "\n".join(SNIPPETS[key].boot)
        assert "migrate(engine)" in boot_text, key


def test_table_less_packages_have_no_boot_lines():
    table_less = {"validation", "storage", "ratelimit", "mcp"}
    for key in table_less:
        assert SNIPPETS[key].boot == (), key


def test_router_packages_include_a_router_in_setup():
    router_owning = {"lookups", "validation", "notifications"}
    for key in router_owning:
        setup_text = "\n".join(SNIPPETS[key].setup)
        assert "include_router" in setup_text, key


def test_router_less_packages_never_include_a_router():
    router_less = {"storage", "ratelimit", "jobs", "access", "workflow", "search"}
    for key in router_less:
        setup_text = "\n".join(SNIPPETS[key].setup)
        assert "include_router" not in setup_text, key


def test_ratelimit_declares_its_own_settings_fields():
    names = {name for name, _, _ in SNIPPETS["ratelimit"].settings_fields}
    assert names == {"rate_limit_enabled", "rate_limit_overrides"}


def test_most_packages_declare_no_extra_settings_fields():
    for key, snippet in SNIPPETS.items():
        if key != "ratelimit":
            assert snippet.settings_fields == (), key
