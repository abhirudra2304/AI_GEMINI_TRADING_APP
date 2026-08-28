import os
import logging
from typing import Optional
import pandas as pd

logger = logging.getLogger(__name__)

def clean_constituent_file(filepath: str):
    """
    Cleans and prunes a historical constituent file by removing redundant entries.

    This function performs two main actions:
    1.  De-duplicates all records to ensure data integrity.
    2.  Prunes historical lists that are identical to the previous entry,
        keeping only the first occurrence and subsequent changes. This reduces
        file size and speeds up loading in the ConstituentProvider.
    """
    if not os.path.exists(filepath):
        print(f"⚠️ File not found: {filepath}. Nothing to clean.")
        logger.warning(f"File not found during cleanup: {filepath}")
        return

    print(f"🧹 Cleaning and pruning {filepath}...")

    try:
        df = pd.read_csv(filepath, parse_dates=['date'])
        if 'date' not in df.columns or 'symbol' not in df.columns:
            print(f"❌ Invalid format for {filepath}. Must have 'date' and 'symbol' columns.")
            return

        initial_rows = len(df)

        # 1. Basic de-duplication and sorting
        df = df.drop_duplicates().sort_values(by=['date', 'symbol']).reset_index(drop=True)
        
        # 2. Pruning logic
        grouped = df.groupby('date')['symbol'].apply(set).sort_index()
        
        if len(grouped) <= 1:
            print("✅ File contains only one date entry. No pruning needed.")
            df.to_csv(filepath, index=False)
            return
            
        dates_to_keep = {grouped.index[0]}
        last_set = grouped.iloc[0]

        for date, current_set in grouped.iloc[1:].items():
            if current_set != last_set:
                dates_to_keep.add(date)
                last_set = current_set
        
        pruned_df = df[df['date'].isin(dates_to_keep)]
        final_rows = len(pruned_df)
        rows_removed = initial_rows - final_rows

        # Overwrite the file with the cleaned data
        pruned_df.to_csv(filepath, index=False)

        print(f"✅ Pruning complete for {filepath}.")
        print(f"   Initial rows: {initial_rows}")
        print(f"   Final rows:   {final_rows}")
        print(f"   Rows removed:   {rows_removed}")

    except Exception as e:
        print(f"❌ An error occurred while cleaning {filepath}: {e}")
        logger.error(f"Failed to clean constituent file {filepath}: {e}", exc_info=True)

def run_cleanup(index_name: Optional[str] = None):
    """
    Finds and cleans constituent files in the data directory.
    """
    data_dir = "data"
    if not os.path.isdir(data_dir):
        print(f"⚠️ Data directory '{data_dir}' not found.")
        return

    if index_name:
        # Clean a specific file
        safe_index_name = index_name.lower().replace(" ", "")
        filepath = os.path.join(data_dir, f"{safe_index_name}_constituents.csv")
        clean_constituent_file(filepath)
    else:
        # Clean all constituent files
        print("🧹 Cleaning all constituent files in the 'data' directory...")
        cleaned_any = False
        for filename in os.listdir(data_dir):
            if filename.endswith("_constituents.csv"):
                filepath = os.path.join(data_dir, filename)
                clean_constituent_file(filepath)
                cleaned_any = True
        if not cleaned_any:
            print("No constituent files found to clean.")
