import inspect
from pathlib import PurePosixPath

from src.cli.commands.demo import demo_cmd


def _default_output_dir() -> str:
    """The --output-dir default declared on demo_cmd (a typer OptionInfo)."""
    param = inspect.signature(demo_cmd).parameters["output_dir"]
    option = param.default
    # typer.Option(...) stores the literal default on `.default`
    return option.default


class TestDemoOutputDir:
    def test_demo_does_not_write_into_committed_fixtures(self):
        """Regression for #87: `raglogs demo` must not overwrite the committed
        sample_data/ fixtures, which the integration test and README depend on
        staying immutable."""
        parts = PurePosixPath(_default_output_dir()).parts
        assert "sample_data" not in parts

    def test_demo_default_is_under_gitignored_data_dir(self):
        """The default lands under data/, which .gitignore already excludes,
        so demo runs leave the working tree clean."""
        parts = PurePosixPath(_default_output_dir()).parts
        assert "data" in parts
