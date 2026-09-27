"""Bundle the independent client locally until it has a published distribution.

The client source remains exclusively in python/src/x2d_gateway. No generated
copy belongs in home_assistant/. This requires no package registry or network.
"""

import argparse
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "x2d-0.1.0.zip"
    with tempfile.TemporaryDirectory(prefix="ha-x2d-package-") as staging:
        stage = Path(staging)
        component = stage / "custom_components" / "x2d"
        shutil.copytree(
            ROOT / "home_assistant/custom_components/x2d", component,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copytree(
            ROOT / "python/src/x2d_gateway", component / "_client",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__main__.py"),
        )
        with ZipFile(archive, "w", ZIP_DEFLATED) as target:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    target.write(path, path.relative_to(stage))
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    print(build(parser.parse_args().output))
