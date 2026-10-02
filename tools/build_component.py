"""Bundle the independent client locally until it has a published distribution.

The client source remains exclusively in python/src/x2d_gateway. Its bundled
copy is generated only in the archive. This requires no registry or network.
"""

import argparse
import json
from pathlib import Path
import shutil
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path, *, hacs: bool = False) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / "custom_components/x2d"
    version = json.loads((source / "manifest.json").read_text())["version"]
    filename = json.loads((ROOT / "hacs.json").read_text())["filename"] if hacs else f"x2d-{version}.zip"
    archive = output / filename
    with tempfile.TemporaryDirectory(prefix="ha-x2d-package-") as staging:
        stage = Path(staging)
        component = stage / "custom_components" / "x2d"
        shutil.copytree(
            source, component,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copytree(
            ROOT / "python/src/x2d_gateway", component / "_client",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__main__.py"),
        )
        with ZipFile(archive, "w", ZIP_DEFLATED) as target:
            archive_root = component if hacs else stage
            for path in sorted(archive_root.rglob("*")):
                if path.is_file():
                    # Fixed ZIP metadata makes builds independent of mtimes/umask.
                    entry = ZipInfo(path.relative_to(archive_root).as_posix())
                    entry.create_system = 3
                    entry.external_attr = 0o100644 << 16
                    target.writestr(entry, path.read_bytes(), compress_type=ZIP_DEFLATED)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--hacs", action="store_true", help="Build the release asset with component files at ZIP root")
    args = parser.parse_args()
    print(build(args.output, hacs=args.hacs))
