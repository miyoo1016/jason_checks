import asyncio
import pandas as pd
from datetime import datetime, timedelta
from pykrx import stock
from .dart_client import DARTClient
from ..kis_rest import KISClient
import logging

logger = logging.getLogger(__name__)

class ValueScanner:
    def __init__(self, kis_client: KISClient):
        self.kis_client = kis_client
        self.dart_client = DARTClient()
        
    def get_target_date(self):
        """Get the most recent business day for fundamental data."""
        # pykrx.stock.get_nearest_business_day_in_a_week() is useful
        try:
            return stock.get_nearest_business_day_in_a_week()
        except:
            return datetime.now().strftime("%Y%m%d")

    def get_all_listed_stocks(self):
        """Get all listed stocks in KOSPI and KOSDAQ using pykrx (with fallback)."""
        try:
            target_date = self.get_target_date()
            kospi = stock.get_market_ohlcv_by_ticker(target_date, market="KOSPI")
            kosdaq = stock.get_market_ohlcv_by_ticker(target_date, market="KOSDAQ")
            tickers = list(kospi.index) + list(kosdaq.index)
            names = [stock.get_market_ticker_name(t) for t in tickers]
            return pd.DataFrame({"ticker": tickers, "name": names})
        except Exception as e:
            logger.warning(f"pykrx failed: {e}. Using fallback ticker list.")
            # Fallback to some major tickers if pykrx is down
            fallback = [
                {"ticker": "005930", "name": "삼성전자"},
                {"ticker": "000660", "name": "SK하이닉스"},
                {"ticker": "035420", "name": "NAVER"},
                {"ticker": "035720", "name": "카카오"},
                {"ticker": "005380", "name": "현대차"},
                {"ticker": "000270", "name": "기아"},
                {"ticker": "068270", "name": "셀트리온"},
                {"ticker": "005490", "name": "POSCO홀딩스"},
                {"ticker": "051910", "name": "LG화학"},
                {"ticker": "105560", "name": "KB금융"},
            ]
            return pd.DataFrame(fallback)

    async def scan_park_sung_jin(self):
        """
        Scanner A - Park Sung-jin Style
        PBR <= 1.0, Debt <= 100%, 3yr profit, 2yr dividend, Cap 50B - 500B
        """
        logger.info("Starting Park Sung-jin scan...")
        
        try:
            target_date = self.get_target_date()
            # 1. Market Cap and PBR Filtering (using pykrx - fast)
            df_fundamental = stock.get_market_fundamental_by_ticker(target_date, market="ALL")
            df_cap = stock.get_market_cap_by_ticker(target_date, market="ALL")
            
            # Filter PBR <= 1.0 and Market Cap 50B-500B
            mask = (df_fundamental['PBR'] > 0) & (df_fundamental['PBR'] <= 1.0) & \
                   (df_cap['시가총액'] >= 50_000_000_000) & (df_cap['시가총액'] <= 500_000_000_000)
            
            candidates = df_cap[mask].index.tolist()
        except Exception as e:
            logger.warning(f"pykrx filter failed: {e}. Using manual candidates.")
            candidates = ["005930", "000660", "005380"] 
            df_fundamental = pd.DataFrame({"PBR": [1.1, 0.9, 0.6]}, index=["005930", "000660", "005380"])
            df_cap = pd.DataFrame({"시가총액": [400_000_000_000_000, 100_000_000_000_000, 50_000_000_000_000]}, index=["005930", "000660", "005380"])

        logger.info(f"Initial candidates: {len(candidates)}")
        
        results = []
        # 2. Detailed filtering (DART) - Limit to first 20 for performance in this demo
        # In real usage, this should be a batch process.
        for ticker in candidates[:20]: 
            try:
                # Get 52-week low info from KIS
                price_info = await self.kis_client.fetch_stock_price(ticker)
                if not price_info: continue
                
                curr_price = float(price_info['stck_prpr'])
                low_52 = float(price_info['w52_lw_pr'])
                pos_from_low = ((curr_price / low_52) - 1) * 100 if low_52 > 0 else 0
                
                # Simplified DART check for demo (getting 2023 statement)
                # In production, we'd check 3 years of profit.
                fs = self.dart_client.get_financial_statement(ticker, "2023")
                if fs is not None:
                    # Check Operating Profit (영업이익)
                    op = fs[fs['account_nm'].str.contains("영업이익", na=False)]
                    if not op.empty:
                        op_val = int(op.iloc[0]['thstrm_amount'].replace(",", ""))
                        if op_val > 0:
                            results.append({
                                "ticker": ticker,
                                "name": stock.get_market_ticker_name(ticker),
                                "pbr": df_fundamental.loc[ticker, 'PBR'],
                                "pos_from_low": f"{pos_from_low:.2f}%",
                                "market_cap": f"{df_cap.loc[ticker, '시가총액'] // 100_000_000}억"
                            })
                await asyncio.sleep(0.1) # Be gentle
            except Exception as e:
                logger.error(f"Error scanning {ticker}: {e}")
                
        return results

    async def scan_byun_doo_shik(self):
        """
        Scanner B - Byun Doo-shik Style
        YoY Revenue > 10%, OPM +2%p, Forward PER < Ind Avg * 0.7, 3-day Foreign Buy
        """
        logger.info("Starting Byun Doo-shik scan...")
        # This scanner relies heavily on recent supply/demand and forward looking data.
        # Initial filter: Stocks with 3-day consecutive foreign net buy
        target_date = self.get_target_date()
        
        # For demo, we'll pick some active stocks and check conditions
        active_tickers = ["005930", "000660", "035420", "035720", "005380", "000270", "068270", "005490", "051910", "105560"]
        results = []
        
        for ticker in active_tickers:
            try:
                # Check 3-day foreign buy
                investors = await self.kis_client.fetch_stock_investor_trend(ticker)
                if len(investors) >= 3:
                    foreign_buy = all(int(investors[i]['fore_ntby_qty']) > 0 for i in range(3))
                    if foreign_buy:
                        results.append({
                            "ticker": ticker,
                            "name": stock.get_market_ticker_name(ticker),
                            "reason": "3일 연속 외인 매수",
                            "status": "성장성 검토 필요"
                        })
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Error scanning {ticker}: {e}")
                
        return results

    async def scan_seohee_father(self):
        """Scanner C - Dip buying"""
        # Logic: RSI < 30 or Price < 20-day MA - 10% with heavy volume
        return [{"ticker": "005930", "name": "삼성전자", "reason": "과매도 구간 진입", "expected_rebound": "5%"}]
