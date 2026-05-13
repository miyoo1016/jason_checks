import os
import zipfile
import io
import xml.etree.ElementTree as ET
import requests
import pandas as pd
from pathlib import Path
from typing import Optional, Dict
from ..config import get_settings

class DARTClient:
    """Client for DART (Open Data Analysis, Retrieval and Transfer System) OpenAPI."""
    
    BASE_URL = "https://opendart.fss.or.kr/api"
    
    def __init__(self):
        settings = get_settings()
        self.api_key = settings.dart_api_key
        self.cache_dir = Path.home() / ".jason_checks" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.corp_code_path = self.cache_dir / "corp_code.csv"
        self._corp_codes: Dict[str, str] = {} # stock_code -> corp_code
        
    def _load_corp_codes(self):
        """Download and load corp_code mapping."""
        if self.corp_code_path.exists():
            df = pd.read_csv(self.corp_code_path, dtype={'stock_code': str, 'corp_code': str})
            # Filter out entries without stock_code (non-listed companies)
            df = df[df['stock_code'].notna()]
            self._corp_codes = dict(zip(df['stock_code'], df['corp_code']))
            return

        print("Downloading DART corp codes...")
        url = f"{self.BASE_URL}/corpCode.xml"
        params = {"crtfc_key": self.api_key}
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            print(f"Failed to download corp codes: {response.status_code}")
            return

        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            xml_data = z.read("CORPCODE.xml")
            
        root = ET.fromstring(xml_data)
        data = []
        for list_tag in root.findall("list"):
            corp_code = list_tag.findtext("corp_code")
            stock_code = list_tag.findtext("stock_code")
            if stock_code and stock_code.strip():
                data.append({"stock_code": stock_code.strip(), "corp_code": corp_code})
        
        df = pd.DataFrame(data)
        df.to_csv(self.corp_code_path, index=False)
        self._corp_codes = dict(zip(df['stock_code'], df['corp_code']))
        print(f"Loaded {len(self._corp_codes)} corp codes.")

    def get_corp_code(self, stock_code: str) -> Optional[str]:
        """Get DART corp_code from 6-digit stock code."""
        if not self._corp_codes:
            self._load_corp_codes()
        return self._corp_codes.get(stock_code)

    def get_financial_statement(self, stock_code: str, year: str, reprt_code: str = "11011") -> Optional[pd.DataFrame]:
        """
        Fetch single account financial statement.
        reprt_code: 11011(사업보고서), 11012(반기), 11013(1분기), 11014(3분기)
        """
        corp_code = self.get_corp_code(stock_code)
        if not corp_code:
            return None
            
        url = f"{self.BASE_URL}/fnlttSinglAcntAll.json"
        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bsns_year": year,
            "reprt_code": reprt_code,
            "fs_div": "CFS" # Consolidated
        }
        
        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "000":
                return pd.DataFrame(data.get("list"))
            else:
                # If CFS fails, try OFS (Separate)
                params["fs_div"] = "OFS"
                response = requests.get(url, params=params)
                data = response.json()
                if data.get("status") == "000":
                    return pd.DataFrame(data.get("list"))
        return None

if __name__ == "__main__":
    client = DARTClient()
    # Test with Samsung Electronics
    df = client.get_financial_statement("005930", "2023")
    if df is not None:
        print(df.head())
    else:
        print("Failed to fetch data")
