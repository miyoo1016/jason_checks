import asyncio
from jason_checks.theme_ranker import rank_themes
from jason_checks.web.app import load_themes, get_expanded_subscription_codes

theme_data = load_themes()
active_themes = rank_themes(theme_data, 4, [])
print("Active Themes:", active_themes)
codes = get_expanded_subscription_codes(theme_data, active_themes, max_codes=16)
print("Expanded Codes:", codes)
