"""Load this repository's local configuration without searching parent folders."""
from pathlib import Path
from dotenv import load_dotenv

def load_env_stack(project_dir=None):
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", override=False)
