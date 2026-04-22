import asyncio
from jason_checks.kis_rest import fetch_current_price

async def main():
    codes = ["005930", "000660", "373220", "207940", "005380", "000270", "068270", "005490", "105560", "055550", "032830", "012330", "028260", "066970", "022100", "035420"]
    for i, code in enumerate(codes):
        data = await fetch_current_price(code)
        print(f"[{i+1}] {code}: {data}")
        await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(main())
