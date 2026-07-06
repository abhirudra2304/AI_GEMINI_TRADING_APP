import re
import os

def clean_target_universe():
    file_path = "config.py"
    
    if not os.path.exists(file_path):
        print(f"❌ Error: {file_path} not found in the current directory!")
        return

    with open(file_path, 'r') as f:
        content = f.read()

    # Regex to find the TARGET_UNIVERSE list block
    match = re.search(r'(TARGET_UNIVERSE\s*=\s*\[)(.*?)(\])', content, re.DOTALL)
    if not match:
        print(f"❌ Error: Could not find 'TARGET_UNIVERSE = [...]' block in {file_path}.")
        return

    prefix = match.group(1)
    list_content = match.group(2)
    suffix = match.group(3)

    # Extract all stock symbols wrapped in quotes
    symbols = re.findall(r'["\']([^"\']+)["\']', list_content)
    
    if not symbols:
        print("⚠️ No stocks found to clean.")
        return

    # Deduplicate while preserving the original order
    seen = set()
    unique_stocks = []
    for stock in symbols:
        stock = stock.upper().strip()
        if stock not in seen and stock:
            seen.add(stock)
            unique_stocks.append(stock)

    removed_count = len(symbols) - len(unique_stocks)

    # Format nicely (8 stocks per line for clean readability)
    formatted_lines = []
    for i in range(0, len(unique_stocks), 8):
        chunk = unique_stocks[i:i+8]
        line = "    " + ", ".join([f'"{s}"' for s in chunk]) + ","
        formatted_lines.append(line)
        
    new_list_content = "\n" + "\n".join(formatted_lines) + "\n"

    # Replace the old messy block with the new clean block
    new_content = content[:match.start()] + prefix + new_list_content + suffix + content[match.end():]

    with open(file_path, 'w') as f:
        f.write(new_content)

    print("=" * 50)
    print(f"✅ Successfully cleaned TARGET_UNIVERSE!")
    print(f"🧹 Removed {removed_count} duplicate stocks.")
    print(f"📈 Total unique stocks remaining: {len(unique_stocks)}")
    print("=" * 50)

if __name__ == "__main__":
    clean_target_universe()