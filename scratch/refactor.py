import re

with open("src/jason_checks/kis_rest.py", "r") as f:
    content = f.read()

# Add get_http_client at the top, after imports
client_code = """
_global_client = None

def get_http_client() -> httpx.AsyncClient:
    global _global_client
    if _global_client is None:
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        _global_client = httpx.AsyncClient(verify=False, limits=limits)
    return _global_client
"""

content = content.replace("logger = structlog.get_logger()", "logger = structlog.get_logger()\n" + client_code)

# Replace the async with blocks
pattern = re.compile(r'([ \t]+)async with httpx\.AsyncClient\(verify=False\) as client:\n([ \t]+)resp = await client\.(get|post)\((.*?)\)', re.MULTILINE)

def replacer(match):
    indent1 = match.group(1)
    indent2 = match.group(2)
    method = match.group(3)
    args = match.group(4)
    # The new code should be indented with indent1
    # Because `async with` is a block, the contents were indented one level deeper.
    # We must dedent the lines that followed the `async with`, but for now, we just replace the block start and let the indentation of `resp = ` remain, which is fine in python (just an extra level of indentation for a block that isn't there? No, we shouldn't do that).
    
    # Actually, it's safer to just replace `async with... as client:` with `client = get_http_client()` and we don't need to change the indentation of the block if we use a dummy `if True:` instead!
    return f"{indent1}client = get_http_client()\n{indent1}if True:\n{indent2}resp = await client.{method}({args})"

content = pattern.sub(replacer, content)

with open("src/jason_checks/kis_rest.py", "w") as f:
    f.write(content)

print("Refactored kis_rest.py")
