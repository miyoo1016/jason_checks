import asyncio
from jason_checks.kis_rest import fetch_stock_investor_trend

async def main():
    codes = ["005930", "000660", "373220", "207940"]
    for code in codes:
        tr = await fetch_stock_investor_trend(code)
        print(f"{code}: {tr}")
        await asyncio.sleep(2.0)

if __name__ == "__main__":
    asyncio.run(main())
