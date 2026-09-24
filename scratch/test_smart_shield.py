def test_smart_shield(asset, distance_pct, mom, current_vol, base_vol, base_min_dist):
    # Option 1: Dynamic Volatility Scaling
    # If current_vol is lower than base, the required distance shrinks.
    vol_ratio = current_vol / base_vol
    
    # We cap the shrinking at 50% of the base distance, and expansion at 150%
    dynamic_min_dist = base_min_dist * vol_ratio
    dynamic_min_dist = max(base_min_dist * 0.5, min(dynamic_min_dist, base_min_dist * 1.5))
    
    # Option 3: Momentum Veto
    # If we are inside the ORIGINAL base buffer, we demand positive momentum (moving away from danger).
    # If mom < 0, it means the price is moving TOWARD the danger line.
    
    reasons = []
    
    if abs(distance_pct) < dynamic_min_dist:
        reasons.append(f"REJECTED: {abs(distance_pct):.4%} < Dynamic Limit {dynamic_min_dist:.4%}")
        return False, reasons
        
    if abs(distance_pct) < base_min_dist and mom <= 0:
        reasons.append(f"REJECTED: Inside Base Buffer ({abs(distance_pct):.4%} < {base_min_dist:.4%}) AND Adverse Momentum ({mom:+.2f})")
        return False, reasons
        
    reasons.append(f"ACCEPTED: Distance {abs(distance_pct):.4%} >= {dynamic_min_dist:.4%}. Momentum: {mom:+.2f}")
    return True, reasons

print("--- TESTING SMART SHIELD ---")
# SOL Base: 0.0020 (0.20%). Base Vol: 2.8%
base_dist = 0.0020
base_vol = 0.028

scenarios = [
    {"name": "Calm Market, Safe Momentum", "dist": 0.0012, "mom": 0.5, "vol": 0.014}, # Vol is half (1.4%). Dist is 0.12%
    {"name": "Calm Market, Adverse Momentum", "dist": 0.0012, "mom": -0.2, "vol": 0.014}, # Same calm market, but price crashing
    {"name": "Chaotic Market, Safe Momentum", "dist": 0.0025, "mom": 0.5, "vol": 0.040}, # Vol is high (4.0%). Dist is 0.25%
    {"name": "Chaotic Market, Danger Close", "dist": 0.0018, "mom": 0.5, "vol": 0.040}, # High vol, but only 0.18% away
]

for s in scenarios:
    passed, msgs = test_smart_shield("SOL", s["dist"], s["mom"], s["vol"], base_vol, base_dist)
    print(f"[{s['name']}] -> {'✅ PASS' if passed else '❌ FAIL'}")
    print(f"   {msgs[0]}")
    print()
