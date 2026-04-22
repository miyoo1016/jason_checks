from jason_checks.web.app import load_themes
themes = load_themes()
for theme_key, theme_data in themes.items():
    for stock in theme_data.get("stocks", []):
        if stock["code"] == "000660":
            print("Found 000660 in", theme_key)
