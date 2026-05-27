import asyncio
import os
import sys

from jason_checks.telegram_notifier import maybe_send_index_alert, _recent_events

async def test_alerts():
    print("--- Test 1: MARKET_CLOSED ---")
    os.environ["KR_TELEGRAM_ENABLED"] = "true"
    os.environ["KR_TELEGRAM_DRY_RUN"] = "true"
    
    # We pass MARKET_CLOSED
    res_closed = await maybe_send_index_alert(
        index_code="KOSDAQ",
        index_name="KOSDAQ",
        change_pct=-3.5,
        price=1100.0,
        threshold_pct=3.0,
        session_status="MARKET_CLOSED"
    )
    print("Result:", res_closed)
    
    # Check history to see if it was skipped and reason="MARKET_CLOSED"
    if _recent_events:
        print("History Event:", _recent_events[0])
    else:
        print("No history event recorded.")
        sys.exit(1)

    print("\n--- Test 2: REGULAR ---")
    res_regular = await maybe_send_index_alert(
        index_code="KOSDAQ",
        index_name="KOSDAQ",
        change_pct=-3.5,
        price=1100.0,
        threshold_pct=3.0,
        session_status="REGULAR"
    )
    print("Result:", res_regular)
    
    print("Done")

if __name__ == "__main__":
    asyncio.run(test_alerts())
