"""One explicit, non-shell environment file for API, worker and diagnostics."""
import os
from pathlib import Path
from dotenv import load_dotenv


def load_config():
    path = Path(os.environ.get("COURSE_MATERIALS_ENV_FILE", Path(__file__).resolve().parents[2] / ".env.course-materials"))
    load_dotenv(path, override=False)
    return str(path.resolve())
