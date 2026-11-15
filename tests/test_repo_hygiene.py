import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_private_runtime_files_and_sqlite_databases_are_not_tracked():
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    )
    tracked = [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    forbidden = []
    for path in tracked:
        name = Path(path).name
        lowered = name.lower()
        if name in {".env", "resume.md"} or lowered.endswith(
            (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3",
             ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")
        ):
            forbidden.append(path)
    assert forbidden == [], f"Private runtime files must not be tracked: {forbidden}"


def test_gitignore_covers_sqlite_sidecar_files():
    ignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "*.db-wal", "*.db-shm", "*.db-journal",
        "*.sqlite-wal", "*.sqlite-shm", "*.sqlite-journal",
    ):
        assert pattern in ignore_text
