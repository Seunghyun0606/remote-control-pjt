import pytest


def test_project_registry_loads(project_registry, project_dir):
    project = project_registry.get("demo")
    assert project.name == "Demo Project"
    assert project.path_for("lightsail-main") == str(project_dir)


def test_disallowed_host(project_registry):
    project = project_registry.get("demo")
    with pytest.raises(ValueError, match="not allowed"):
        project.path_for("desktop-main")
