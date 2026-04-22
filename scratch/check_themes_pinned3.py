import urllib.request
import urllib.parse
import json

try:
    url = "http://127.0.0.1:8000/api/themes?pinned=" + urllib.parse.quote("반도체")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        themes = data.get("themes", {})
        print("Stocks in 반도체:")
        for stock in themes.get("반도체", {}).get("stocks", []):
            print(f"- {stock['name']} ({stock['code']})")
except Exception as e:
    print("Error:", e)
