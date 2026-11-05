"""Every package module can be imported in a fresh interpreter."""

import pkgutil
import subprocess
import sys

import pytest

import semantica_dataagent


@pytest.mark.parametrize(
    "module",
    sorted(
        info.name
        for info in pkgutil.walk_packages(
            semantica_dataagent.__path__, semantica_dataagent.__name__ + "."
        )
    ),
)
def test_module_imports_independently(module):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; importlib.import_module(sys.argv[1])",
            module,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"{module} failed to import:\n{result.stderr}"
