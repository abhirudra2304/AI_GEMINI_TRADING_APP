import sys
import os

def add_stocks_to_universe(stocks_input):
    file_path = "config.py"
    
    if not os.path.exists(file_path):
        print(f"❌ Error: {file_path} not found in the current directory!")
        return

    # Handle both comma-separated and space-separated inputs smoothly
    raw_stocks = " ".join(stocks_input).replace(',', ' ').split()
    new_stocks = [s.strip().upper() for s in raw_stocks if s.strip()]

    if not new_stocks:
        print("⚠️ Please provide at least one stock symbol.")
        print("Usage Example: python add_stock.py RELIANCE ZOMATO HAL")
        return

    # Format into valid Python strings (e.g., '"RELIANCE", "ZOMATO", ')
    formatted_injection = ", ".join([f'"{s}"' for s in new_stocks]) + ","

    with open(file_path, 'r') as f:
        content = f.read()

    if "TARGET_UNIVERSE = [" in content:
        # Inject right after the opening bracket
        new_content = content.replace("TARGET_UNIVERSE = [", f"TARGET_UNIVERSE = [\n    {formatted_injection}")
        
        with open(file_path, 'w') as f:
            f.write(new_content)
            
        print(f"✅ Successfully added to TARGET_UNIVERSE: {', '.join(new_stocks)}")
    else:
        print(f"❌ Error: Could not find 'TARGET_UNIVERSE = [' in {file_path}.")

if __name__ == "__main__":
    add_stocks_to_universe(sys.argv[1:])