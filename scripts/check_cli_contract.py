"""Verify the frozen v0.2 ten-command public CLI contract."""

import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from traceverdict.cli import app

EXPECTED = {"run", "suite", "compare", "report", "inject", "replay", "selftest", "quick", "baseline", "ingest"}


def main() -> int:
    result = CliRunner().invoke(app, ["--help"])
    if result.exit_code != 0:
        print(result.output)
        return result.exit_code or 1
    missing = sorted(name for name in EXPECTED if name not in result.output)
    if missing:
        print(f"missing commands: {missing}")
        return 1
    print(result.output)
    print("CLI CONTRACT PASSED: ten commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
