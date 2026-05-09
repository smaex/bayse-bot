import csv
import json
import os
import database
from datetime import datetime

DATA_DIR = "data/exports"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

def export_to_csv():
    print("Fetching recordings from database...")
    # Fetch all recordings (adjust limit as needed)
    recordings = database.get_recordings(limit=50000)
    
    if not recordings:
        print("No recordings found in database.")
        return

    filename = f"{DATA_DIR}/export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "created_at", "type", "asset", "data_json"])
        
        for r in recordings:
            writer.writerow([
                r["id"],
                r["created_at"],
                r["type"],
                r["asset"],
                r["data_json"]
            ])
            
    print(f"✅ Success! Exported {len(recordings)} rows to {filename}")

if __name__ == "__main__":
    export_to_csv()
