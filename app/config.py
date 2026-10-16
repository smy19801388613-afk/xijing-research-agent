import os
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("RESEARCH_DATA_DIR", ROOT / "var"))
FIXTURES = ROOT / "examples"
VERSION = "0.4.8"

def legacy_settings():
    # Read existing credentials locally; never copy them into the product or UI.
    legacy = {} if os.environ.get("RESEARCH_COMPETITION_MODE", "").lower() in ("1","true","yes") else dotenv_values(Path.home() / ".vibe-trading" / ".env")
    own = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}
    values = {**legacy, **own, **os.environ}
    return {
        "token": values.get("TUSHARE_TOKEN", ""),
        "key": values.get("DEEPSEEK_API_KEY", ""),
        "base": values.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "model": values.get("LANGCHAIN_MODEL_NAME", "deepseek-chat"),
    }

def settings():
    from .connections import runtime_settings
    return runtime_settings()
