from __future__ import annotations

from click.testing import CliRunner

from drupal_remedy.cli import cli


def test_cli_imports_and_renders_help():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "Drupal accessibility remediation tool." in result.output
    assert "serve-http" in result.output
