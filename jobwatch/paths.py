"""
paths.py
========
This module answers one question: WHERE does JobWatch keep your personal data?

The golden rule of this project: your personal data (which companies you track,
your search history, your interests) lives in a folder that is PHYSICALLY OUTSIDE
the code project, so it can never accidentally end up in git / on the internet.

By default that folder is:  ~/JobWatchData
("~" means your Mac home folder, e.g. /Users/yourname)

You never have to create this folder yourself. The app makes it on first run
and tells you where it is, in plain language.
"""

import os
from pathlib import Path

# The name of your data folder, placed in your home directory.
# (Advanced: you could point this elsewhere via an environment variable,
#  but you never need to.)
DATA_DIR_NAME = "JobWatchData"


def data_root() -> Path:
    """Return the path to your external data folder (does not create it)."""
    env_override = os.environ.get("JOBWATCH_DATA_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (Path.home() / DATA_DIR_NAME).resolve()


def project_root() -> Path:
    """Return the path to the code project (this repo). Data must NOT live here."""
    return Path(__file__).resolve().parent.parent


def _is_inside_project(path: Path) -> bool:
    """Safety check: is the given path inside the code repo? It must never be."""
    try:
        path.resolve().relative_to(project_root())
        return True
    except ValueError:
        return False


def ensure_data_dirs() -> dict:
    """
    Create the external data folder (and its sub-folders) if missing.
    Returns a small report so the app can confirm to the user in plain language.

    Refuses to run if the data folder would sit inside the code repo, because
    that would defeat the whole git-safety design.
    """
    root = data_root()

    if _is_inside_project(root):
        raise RuntimeError(
            "Refusing to store data inside the code project. "
            "Your personal data must live outside the repo for safety. "
            f"Tried to use: {root}"
        )

    sub_dirs = {
        "root": root,
        "snapshots": root / "snapshots",
    }

    created = []
    for _, p in sub_dirs.items():
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(p)

    return {
        "data_folder": str(root),
        "created_now": [str(p) for p in created],
        "already_existed": not created,
    }


def friendly_confirmation() -> str:
    """A plain-language sentence the app can show the user about where data lives."""
    report = ensure_data_dirs()
    where = report["data_folder"]
    if report["already_existed"]:
        return (f"Your JobWatch data is stored safely at:\n  {where}\n"
                "(It's outside the code, so it can never end up online. "
                "You won't need to touch this folder.)")
    return (f"I've created your data folder at:\n  {where}\n"
            "Everything JobWatch saves about your job search lives here, "
            "safely outside the code. You won't need to touch this folder.")


# Allow running this file directly to test the setup:
#   python3 jobwatch/paths.py
if __name__ == "__main__":
    print(friendly_confirmation())
    print("\nProject (code) folder:", project_root())
    print("Data folder is inside project?:", _is_inside_project(data_root()),
          "(must be False)")
