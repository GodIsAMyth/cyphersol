#!/usr/bin/env python3
"""Root launcher script for the CypherSOL Unified Application."""

import os
import subprocess
import sys

sys.dont_write_bytecode = True

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def _maybe_reexec_in_venv(root_dir: str) -> None:
    """Switch to the local .venv interpreter automatically if present."""
    if os.environ.get("CYPHERSOL_SKIP_VENV_REEXEC") == "1":
        return
    venv_dir = os.path.abspath(os.path.join(root_dir, ".venv"))
    if os.path.abspath(sys.prefix) == venv_dir:
        return
    venv_py = (
        os.path.join(venv_dir, "Scripts", "python.exe")
        if os.name == "nt"
        else os.path.join(venv_dir, "bin", "python")
    )
    if not os.path.isfile(venv_py):
        return
    os.environ["CYPHERSOL_SKIP_VENV_REEXEC"] = "1"
    cmd = [venv_py, "-B", __file__, *sys.argv[1:]]
    if os.name == "nt":
        raise SystemExit(subprocess.call(cmd))
    os.execv(venv_py, cmd)


_maybe_reexec_in_venv(ROOT_DIR)

SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.cli import main

if __name__ == "__main__":
    main()
