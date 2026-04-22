import asyncio
from jason_checks.kis_rest import fetch_current_price

async def main():
    data = await fetch_current_price("000660")
    print(f"Hynix Price Data: {data}")

if __name__ == "__main__":
    asyncio.run(main())
