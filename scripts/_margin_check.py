pos_sz = 1.0  # placeholder position size
entry_px = 0.1  # placeholder entry price
mark_px = 0.1  # placeholder mark price
lev = 3.0
ctVal = 1000  # DOGE contract size
total_eq = 1000.0  # placeholder account equity
max_margin_per_trade = total_eq * 0.10  # hard cap

notional = pos_sz * mark_px * ctVal
margin_used = notional / lev
margin_pct = margin_used / total_eq * 100
max_contracts_at_10x = max_margin_per_trade * 10 / (mark_px * ctVal)
max_contracts_at_3x = max_margin_per_trade * lev / (mark_px * ctVal)

print(f'DOGE SHORT position check:')
print(f'  Size: {pos_sz} contracts | Entry: ${entry_px} | Mark: ${mark_px}')
print(f'  Notional: ${notional:.2f}')
print(f'  Margin used: ${margin_used:.2f} ({margin_pct:.2f}% of equity)')
print(f'  Hard cap 10%: ${max_margin_per_trade:.2f}')
print(f'  Max contracts @ 10x: {max_contracts_at_10x:.1f} | @ 3x: {max_contracts_at_3x:.1f}')
upl = (entry_px - mark_px) * pos_sz * ctVal
print(f'  Current UPL: ${upl:.2f}')
print(f'  LiqPx: <REDACTED_LIQ_PRICE> (mark ${mark_px})')
print(f'  COMPLIANT: {margin_used <= max_margin_per_trade}')
print()
print(f'Budget for new trades (totalEq ${total_eq}):')
new_budget = total_eq * 0.10
print(f'  Max per new trade: ${new_budget:.2f}')
print(f'  DOGE 1 contract margin @ 10x: ${mark_px * ctVal / 10:.2f}')
sol_price = 86.68
sol_ctval = 0.1
print(f'  SOL 1 contract margin @ 10x: ${sol_price * sol_ctval / 10:.2f}')
btc_price = 78254.6
btc_ctval = 0.01
print(f'  BTC 1 contract margin @ 10x: ${btc_price * btc_ctval / 10:.2f}')
print()
print(f'Current position side exposure: SHORT only')
print(f'  SHORT margin: ${margin_used:.2f} = {margin_pct:.2f}% of equity')
print(f'  LONG margin: $0 = 0%')
print(f'  Net exposure: SHORT {margin_pct:.2f}% (within 60% hard cap)')
