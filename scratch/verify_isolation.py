import sys
import os
import asyncio
import logging

# Add parent dir to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy

async def verify():
    print("=== Verifying MarketState Isolation ===\n")
    
    # 1. Update Global State (Live)
    strategy.update_price_history("BTC", 60000.0) # uses global_state
    
    # 2. Update Simulated State
    sim_state = strategy.MarketState()
    strategy.update_price_history("BTC", 50000.0, state=sim_state)
    
    global_price = strategy.global_state.kalman_state["BTC"]["x"][0]
    sim_price = sim_state.kalman_state["BTC"]["x"][0]
    
    print(f"Global Kalman BTC: {global_price}")
    print(f"Simulated Kalman BTC: {sim_price}")
    
    if global_price != sim_price:
        print("\n✅ Isolation Verified: Global and Simulated states are independent.")
    else:
        print("\n❌ Isolation Failed: States are leaking!")

    # 3. Test GARCH isolation
    strategy.update_price_history("BTC", 60100.0) # Global shock
    strategy.update_price_history("BTC", 50001.0, state=sim_state) # Sim minor move
    
    global_var = strategy.global_state.garch_state["BTC"]["var"]
    sim_var = sim_state.garch_state["BTC"]["var"]
    
    print(f"Global GARCH Var: {global_var:.10f}")
    print(f"Simulated GARCH Var: {sim_var:.10f}")
    
    if global_var != sim_var:
        print("✅ GARCH Isolation Verified.")
    else:
        print("❌ GARCH Isolation Failed.")

if __name__ == "__main__":
    asyncio.run(verify())
