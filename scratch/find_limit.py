import asyncio
from jason_checks.kis_rest import fetch_current_price

async def test_delay(delay):
    print(f"\n--- Testing delay: {delay}s ---")
    codes = ["005930", "000660", "373220", "207940"]
    for i, code in enumerate(codes):
        data = await fetch_current_price(code)
        if data is None:
            print(f"[{i+1}] {code}: FAILED")
            return False
        print(f"[{i+1}] {code}: OK")
        await asyncio.sleep(delay)
    return True

async def main():
    for d in [0.05, 0.2, 0.35, 0.55]:
        success = await test_delay(d)
        if success:
            print(f"Success at {d}s")
            break
        await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(main())
