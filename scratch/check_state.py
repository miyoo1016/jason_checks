import urllib.request
import json

try:
    req = urllib.request.Request("http://127.0.0.1:8000/api/state")
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        print("Visible codes:", data.get("visible_codes", []))
        if "000660" in data.get("stocks", {}):
            print("Hynix State:", data["stocks"]["000660"])
        else:
            print("Hynix not in state!")
except Exception as e:
    print("Error:", e)
