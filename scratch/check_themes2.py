import urllib.request
import json

try:
    req = urllib.request.Request("http://127.0.0.1:8000/api/themes")
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        print("Themes keys:", list(data.get("themes", {}).keys()))
except Exception as e:
    print("Error:", e)
