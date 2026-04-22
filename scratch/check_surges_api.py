import urllib.request
import json

try:
    req = urllib.request.Request("http://127.0.0.1:8000/api/surges")
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        surges = data.get("surges", [])
        hynix_found = False
        for stock in surges:
            if stock["code"] == "000660":
                print("Hynix in surges:", stock)
                hynix_found = True
        if not hynix_found:
            print("Hynix NOT found in /api/surges")
except Exception as e:
    print("Error:", e)
