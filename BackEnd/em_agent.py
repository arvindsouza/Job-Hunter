"""Engineering Manager LinkedIn scout. Scores jobs against the SEM resume."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import EM_CONFIG, run_agent

if __name__ == "__main__":
    raise SystemExit(run_agent(EM_CONFIG))
