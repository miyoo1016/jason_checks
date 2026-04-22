import urllib.request
import json

try:
    req = urllib.request.Request("http://127.0.0.1:8000/api/themes")
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        themes = data.get("themes", {})
        hynix_found = False
        for theme_name, theme_data in themes.items():
            for stock in theme_data.get("stocks", []):
                if stock["code"] == "000660":
                    print(f"Hynix in {theme_name}:", stock)
                    hynix_found = True
        if not hynix_found:
            print("Hynix NOT found in /api/themes")
except Exception as e:
    print("Error:", e)
