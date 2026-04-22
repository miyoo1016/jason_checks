import urllib.request
import urllib.parse
import json

try:
    url = "http://127.0.0.1:8000/api/themes?pinned=" + urllib.parse.quote("반도체")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        themes = data.get("themes", {})
        for theme_name, theme_data in themes.items():
            for stock in theme_data.get("stocks", []):
                if stock["code"] == "000660":
                    print(f"Hynix in {theme_name}:", stock)
except Exception as e:
    print("Error:", e)
