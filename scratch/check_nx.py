import asyncio
from jason_checks.kis_rest import fetch_current_price
from jason_checks.state import app_state

async def main():
    print("Fetching J...")
    j = await fetch_current_price("005930")
    print("J:", j)
    
if __name__ == "__main__":
    asyncio.run(main())
