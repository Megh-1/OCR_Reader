"""Where the tesseract binary is found: env var, then the Windows default, then PATH (which is how the Linux image finds it)."""

import app.config as config
from app.config import resolve_tesseract_cmd

WINDOWS_DEFAULT = "C:" + chr(92) + "Program Files" + chr(92) + "Tesseract-OCR" + chr(92) + "tesseract.exe"


def nothing_on_disk(path):
    return False


def only(path_that_exists):
    return lambda path: path == path_that_exists


def test_the_windows_default_is_exactly_the_installer_path():
    """A backslash typo here (\\t becomes a TAB) silently breaks OCR on the Windows dev machine."""
    assert config._DEFAULT_WINDOWS_TESSERACT == WINDOWS_DEFAULT


def test_the_environment_variable_wins_over_everything():
    found = resolve_tesseract_cmd(
        env={"TESSERACT_CMD": "/opt/custom/tesseract"}, exists=only(WINDOWS_DEFAULT), which=lambda name: "/usr/bin/tesseract"
    )
    assert found == "/opt/custom/tesseract"


def test_the_windows_default_is_used_when_it_exists_even_if_path_has_one():
    """So the Windows dev machine resolves exactly as it did before PATH was consulted at all."""
    found = resolve_tesseract_cmd(env={}, exists=only(WINDOWS_DEFAULT), which=lambda name: "/usr/bin/tesseract")
    assert found == WINDOWS_DEFAULT


def test_on_linux_it_resolves_to_the_tesseract_on_path():
    """The Docker image: no Windows path, apt puts tesseract at /usr/bin. An actual path, not None."""
    asked = []

    def which(name):
        asked.append(name)
        return "/usr/bin/tesseract"

    assert resolve_tesseract_cmd(env={}, exists=nothing_on_disk, which=which) == "/usr/bin/tesseract"
    assert asked == ["tesseract"]


def test_it_is_none_when_tesseract_is_nowhere():
    assert resolve_tesseract_cmd(env={}, exists=nothing_on_disk, which=lambda name: None) is None


def test_an_empty_environment_variable_is_ignored():
    assert resolve_tesseract_cmd(env={"TESSERACT_CMD": ""}, exists=nothing_on_disk, which=lambda name: "/usr/bin/tesseract") == "/usr/bin/tesseract"


def test_this_machine_resolves_a_real_binary():
    """Whatever the platform, the suite needs OCR: the module-level value must be a usable path."""
    import os

    assert config.TESSERACT_CMD and os.path.exists(config.TESSERACT_CMD)
