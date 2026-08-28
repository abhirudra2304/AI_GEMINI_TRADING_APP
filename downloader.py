import os
import logging
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# Base URL for historical constituents from Nifty Indices
BASE_URL = "https://niftyindices.com/IndexConstituent/ind_{index_name}list_{date_str}.csv"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive',
}

def download_constituents_for_date(index_name: str, date: datetime) -> pd.DataFrame:
    """Downloads constituents for a given index and date."""
    # The URL requires lowercase index name with no spaces
    index_name_formatted = index_name.lower().replace(" ", "")
    date_str = date.strftime("%d%m%Y")
    url = BASE_URL.format(index_name=index_name_formatted, date_str=date_str)
    
    try:
        # Using a session object can be slightly more efficient for multiple requests
        with requests.Session() as s:
            response = s.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        
        content = response.text
        lines = content.splitlines()
        header_line_index = -1
        
        # More robust header detection
        for i, line in enumerate(lines):
            # Header usually contains "Symbol", "Series", "ISIN Code"
            if all(x in line for x in ['"Symbol"', '"Series"', '"ISIN Code"']):
                header_line_index = i
                break
        
        if header_line_index == -1:
            logger.debug(f"Could not find CSV header in response for {date.date()} from {url}")
            return pd.DataFrame()

        csv_data = "\n".join(lines[header_line_index:])
        # Use StringIO to treat the string as a file
        df = pd.read_csv(StringIO(csv_data))
        
        # Clean up column names (remove quotes and extra spaces)
        df.columns = df.columns.str.strip().str.replace('"', '')
        
        if 'Symbol' in df.columns:
            return df[['Symbol']]
        else:
            logger.warning(f"'Symbol' column not found for {date.date()} after parsing.")
            return pd.DataFrame()

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.debug(f"No data available for {index_name} on {date.date()} (404 Not Found).")
        else:
            logger.warning(f"HTTP error downloading for {date.date()}: {e}")
        return pd.DataFrame()
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed for {date.date()}: {e}")
        return pd.DataFrame()

def download_historical_constituents(
    index_name: str = "nifty500",
    start_year: int = 2020,
    end_year: int = datetime.now().year,
    output_path: str = "data/nifty500_constituents.csv"
):
    """
    Downloads historical constituents for a given index and saves them to a CSV.
    It iterates through month-end dates to build a historical record.
    """
    all_constituents = []
    
    print(f"🚀 Starting download of historical constituents for '{index_name}' from {start_year} to {end_year}...")
    print("Note: This may take a few minutes as it checks each month-end.")
    
    dates_to_check = []
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # Get the last day of the month
            date = datetime(year, month, 1) + pd.offsets.MonthEnd(0)
            if date < datetime.now():
                dates_to_check.append(date)

    total_dates = len(dates_to_check)
    for i, date in enumerate(dates_to_check):
        progress = f"[{i+1}/{total_dates}]"
        print(f"  {progress} Fetching for {date.strftime('%Y-%m-%d')}...", end='\r')
        df = download_constituents_for_date(index_name, date)
        
        if not df.empty:
            symbols = df['Symbol'].tolist()
            for symbol in symbols:
                all_constituents.append({'date': date.strftime('%Y-%m-%d'), 'symbol': symbol})
            print(f"  {progress} Fetching for {date.strftime('%Y-%m-%d')}... ✅ Found {len(symbols)} constituents.")
        else:
            print(f"  {progress} Fetching for {date.strftime('%Y-%m-%d')}... ❌ No data found.")
    
    if not all_constituents:
        print("\n⚠️ No constituent data was downloaded. The output file will not be created/updated.")
        return

    final_df = pd.DataFrame(all_constituents).drop_duplicates().sort_values(by=['date', 'symbol'])
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    final_df.to_csv(output_path, index=False)
    
    print("\n" + "="*50)
    print(f"✅ Download complete!")
    print(f"💾 Saved {len(final_df)} total records to '{output_path}'.")
    print("You can now run `python historical_generator.py` for a bias-free backtest.")
    print("="*50)
