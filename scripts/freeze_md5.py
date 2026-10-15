"""Create a local submission snapshot and print its MD5. Python standard library only.

Run after the final build/test. Keep the resulting ZIP unchanged for demo verification.
This snapshots files on disk; it does not verify installed/flashed device contents.
"""

import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("firmware", "laptop", "ultra96", "visualizer"))
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="CG4002 repository root")
    parser.add_argument("--apk", type=Path, action="append", default=[],
                        help="Exact final APK used on a phone; repeat for separate P1/P2 builds")
    parser.add_argument("--output", type=Path, required=True, help="New ZIP path; never overwritten")
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "shared").is_dir() or not (root / "comms").is_dir():
        parser.error("Run from the CG4002 repository root, or pass --root.")

    unity = "visualizer"
    if args.role == "firmware":
        sources = ["hardware/gateway/src", "hardware/gateway/include", "hardware/gateway/scripts",
                   "hardware/gateway/platformio.ini", "hardware/2 players", "comms/firmware/src",
                   "comms/firmware/lib", "shared/protocol"]
        # Preserve available build images as additional evidence, not build caches/toolchains.
        for env in ("p1_hub", "p2_hub", "p1_torso", "p2_torso", "p1_left", "p2_left"):
            build = root / "hardware/gateway/.pio/build" / env
            sources.extend(str(p.relative_to(root)) for p in sorted(build.glob("*.bin")))
    elif args.role in ("laptop", "ultra96"):
        sources = ["comms/src", "comms/configs", "comms/scripts", "comms/pyproject.toml", "shared"]
        if args.role == "ultra96":
            sources.append("ai")
    else:
        if not args.apk:
            parser.error("Visualizer requires --apk with the final APK used on the phone.")
        sources = [f"{unity}/Assets", f"{unity}/Packages", f"{unity}/ProjectSettings", "shared"]

    files = {}
    for source in sources:
        path = root / source
        if not path.exists():
            parser.error(f"Missing source: {path}")
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            relative = candidate.relative_to(root)
            if (candidate.is_file() and not SKIP_DIRS.intersection(relative.parts)
                    and candidate.suffix not in (".pyc", ".pyo")):
                files[relative.as_posix()] = candidate
    for index, apk in enumerate(args.apk, 1):
        apk = apk.resolve()
        if not apk.is_file() or apk.suffix.lower() != ".apk":
            parser.error(f"APK does not exist or is not an .apk: {apk}")
        files[f"APKs/{index}/{apk.name}"] = apk

    output = args.output.resolve()
    receipt = output.with_suffix(".md5.txt")
    if output.exists() or receipt.exists():
        parser.error("Output already exists. Keep the frozen ZIP and choose a new filename.")
    if output in files.values() or any(
        (root / source).is_dir() and (root / source) in output.parents for source in sources
    ):
        parser.error("Place the output ZIP outside the included source folders.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, source in sorted(files.items()):
            archive.write(source, name)
    with ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP verification failed: {bad}")
    digest = hashlib.md5()
    with output.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    result = (f"Category: {args.role}\n"
              f"Created: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
              f"ZIP: {output.name}\n"
              f"Files: {len(files)}\n"
              f"Bytes: {output.stat().st_size}\n"
              f"MD5: {digest.hexdigest()}\n")
    receipt.write_text(result, encoding="utf-8")
    print(result)
    print(f"ZIP saved: {output}\nReceipt saved: {receipt}")


if __name__ == "__main__":
    main()
