"""Reject release tags whose package, lock file or changelog disagrees."""

from pathlib import Path
import re
import sys
import tomllib


def validate_release(tag: str, project: dict, lock: dict, changelog: str) -> None:
    """Validate a stable semantic version without modifying release inputs."""
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("Release tag must be v<major>.<minor>.<patch>.")
    version = tag[1:]
    package = project["project"]
    if package["version"] != version:
        raise ValueError(f"Tag {tag} does not match project version {package['version']}.")
    locked = [entry for entry in lock["package"] if entry["name"] == package["name"]]
    if len(locked) != 1 or locked[0]["version"] != version:
        raise ValueError(f"Lock file must contain exactly one {package['name']} at {version}.")
    if not re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", changelog, re.MULTILINE):
        raise ValueError(f"CHANGELOG.md is missing a dated entry for {version}.")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/check_release.py v<major>.<minor>.<patch>")
    try:
        validate_release(
            sys.argv[1],
            tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")),
            tomllib.loads((root / "uv.lock").read_text(encoding="utf-8")),
            (root / "CHANGELOG.md").read_text(encoding="utf-8"),
        )
    except (ValueError, KeyError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Release metadata matches {sys.argv[1]}.")


if __name__ == "__main__":
    main()
