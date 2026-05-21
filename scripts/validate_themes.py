import os
import sys
import asyncio
import yaml
import json
from collections import defaultdict
from datetime import datetime

# Add src to sys.path to allow importing jason_checks
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from jason_checks.kis_rest import fetch_current_price

THEMES_YAML_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../themes.yaml"))
REPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/reports"))

async def main():
    if not os.path.exists(THEMES_YAML_PATH):
        print(f"File not found: {THEMES_YAML_PATH}")
        sys.exit(1)
        
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    with open(THEMES_YAML_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        
    themes = data.get("themes", {})
    
    code_to_names = defaultdict(set)
    name_to_codes = defaultdict(set)
    code_to_sectors = defaultdict(list)
    name_to_sectors = defaultdict(list)
    
    # 1. Parsing and validation
    invalid_formats = []
    
    for sector_id, sector_data in themes.items():
        stocks = sector_data.get("stocks", [])
        for stock in stocks:
            code = str(stock.get("code", "")).strip()
            name = str(stock.get("name", "")).strip()
            
            if not code or len(code) != 6 or not code.isdigit():
                invalid_formats.append({"code": code, "name": name, "sector": sector_id})
                
            code_to_names[code].add(name)
            name_to_codes[name].add(code)
            code_to_sectors[code].append(sector_id)
            name_to_sectors[name].append(sector_id)
            
    # Duplicates & mismatches
    duplicate_codes = []
    for code, sectors in code_to_sectors.items():
        if len(sectors) > 1:
            duplicate_codes.append({"code": code, "sectors": sectors})
            
    duplicate_names = []
    for name, sectors in name_to_sectors.items():
        if len(sectors) > 1:
            duplicate_names.append({"name": name, "sectors": sectors})
            
    name_mismatches = []
    for code, names in code_to_names.items():
        if len(names) > 1:
            name_mismatches.append({"code": code, "names": list(names)})
            
    code_mismatches = []
    for name, codes in name_to_codes.items():
        if len(codes) > 1:
            code_mismatches.append({"name": name, "codes": list(codes)})
            
    # API Checks
    all_codes = list(code_to_sectors.keys())
    print(f"Total unique codes to check: {len(all_codes)}")
    
    zero_price_codes = []
    invalid_symbol_codes = []
    valid_count = 0
    
    for code in all_codes:
        # Skip badly formatted codes for API
        if not code or len(code) != 6 or not code.isdigit():
            continue
            
        print(f"Fetching {code}...")
        try:
            result = await fetch_current_price(code)
            if result is None:
                invalid_symbol_codes.append(code)
            else:
                price = result.get("price", 0)
                if price <= 0:
                    zero_price_codes.append(code)
                else:
                    valid_count += 1
        except Exception as e:
            invalid_symbol_codes.append(code)
            print(f"Error fetching {code}: {e}")
            
        await asyncio.sleep(0.6) # 0.5~1 second sleep
        
    report = {
        "generated_at": datetime.now().isoformat(),
        "total_unique_codes": len(all_codes),
        "valid_price_count": valid_count,
        "invalid_formats": invalid_formats,
        "duplicate_codes_in_multiple_sectors": duplicate_codes,
        "duplicate_names_in_multiple_sectors": duplicate_names,
        "name_mismatches": name_mismatches,
        "code_mismatches": code_mismatches,
        "zero_price_codes": zero_price_codes,
        "invalid_symbol_codes": invalid_symbol_codes,
    }
    
    json_path = os.path.join(REPORTS_DIR, "theme_validation_report.json")
    md_path = os.path.join(REPORTS_DIR, "theme_validation_report.md")
    
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Theme Validation Report\n\n")
        f.write(f"- **Generated At**: {report['generated_at']}\n")
        f.write(f"- **Total Unique Codes**: {report['total_unique_codes']}\n")
        f.write(f"- **Valid Price Codes**: {report['valid_price_count']}\n\n")
        
        f.write("## 1. Invalid Formats (Not 6 digits)\n")
        for item in invalid_formats:
            f.write(f"- `{item['code']}` ({item['name']}) in sector: {item['sector']}\n")
        if not invalid_formats: f.write("- None\n")
            
        f.write("\n## 2. Duplicate Codes (in multiple sectors)\n")
        for item in duplicate_codes:
            f.write(f"- `{item['code']}` found in: {', '.join(item['sectors'])}\n")
        if not duplicate_codes: f.write("- None\n")
        
        f.write("\n## 3. Duplicate Names (in multiple sectors)\n")
        for item in duplicate_names:
            f.write(f"- `{item['name']}` found in: {', '.join(item['sectors'])}\n")
        if not duplicate_names: f.write("- None\n")
            
        f.write("\n## 4. Name Mismatches (1 Code -> N Names)\n")
        for item in name_mismatches:
            f.write(f"- `{item['code']}` mapped to: {', '.join(item['names'])}\n")
        if not name_mismatches: f.write("- None\n")
            
        f.write("\n## 5. Code Mismatches (1 Name -> N Codes)\n")
        for item in code_mismatches:
            f.write(f"- `{item['name']}` mapped to: {', '.join(item['codes'])}\n")
        if not code_mismatches: f.write("- None\n")
            
        f.write("\n## 6. ZERO_PRICE Codes\n")
        for c in zero_price_codes:
            f.write(f"- `{c}` (Names: {', '.join(code_to_names[c])})\n")
        if not zero_price_codes: f.write("- None\n")
            
        f.write("\n## 7. INVALID_SYMBOL Codes (API failed)\n")
        for c in invalid_symbol_codes:
            f.write(f"- `{c}` (Names: {', '.join(code_to_names.get(c, []))})\n")
        if not invalid_symbol_codes: f.write("- None\n")
            
    print(f"Validation complete. Reports saved to {REPORTS_DIR}")

if __name__ == "__main__":
    asyncio.run(main())
