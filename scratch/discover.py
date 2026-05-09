import asyncio
import os
import logging
import sys
from client import BayseClient
from scanner import discover_series

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

async def main():
    pub = os.getenv("BAYSE_PUBLIC_KEY")
    sec = os.getenv("BAYSE_SECRET_KEY")
    if not pub or not sec:
        print("Error: BAYSE_PUBLIC_KEY and BAYSE_SECRET_KEY must be set in environment.")
        return
    
    client = BayseClient(pub, sec)
    await discover_series(client)

if __name__ == "__main__":
    asyncio.run(main())
