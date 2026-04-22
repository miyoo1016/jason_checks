import asyncio
from jason_checks.web.app import app
from jason_checks.state import app_state
from jason_checks.theme_ranker import rank_themes
import json

print("Theme Data keys:", list(app.theme_data.keys()) if hasattr(app, "theme_data") else "No theme data")
print("Active themes:", rank_themes(app.theme_data, 4, []) if hasattr(app, "theme_data") else "No theme data")

