#!/usr/bin/env python3.12
"""close.py - Close ALL open positions across ALL accounts immediately."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from executor import sender_for
from spot import read_trades, read_accounts
from config import setup_logging

log = setup_logging(__name__)


def close_all_accounts() -> None:
    """Close all positions in all accounts."""
    accounts = read_accounts()
    
    for acc_num in (1, 2):
        login = accounts.get(acc_num, {}).get("login")
        if not login:
            print(f"Account {acc_num}: no login configured, skipping")
            continue
            
        print(f"\n=== Account {acc_num} (login: {login}) ===")
        trades = read_trades(login)
        
        if not trades:
            print(f"  No open positions")
            continue
            
        cmd = sender_for(acc_num)
        for t in trades:
            ticket = t["ticket"]
            symbol = t["symbol"]
            side = t["side"]
            volume = t["volume"]
            print(f"  Closing #{ticket} {symbol} {side} {volume}...", end=" ")
            
            ok, detail = cmd.close_position(ticket)
            if ok:
                print(f"OK - {detail}")
            else:
                print(f"FAILED - {detail}")
        
        # Also send CLOSEALL as backup
        print(f"  Sending CLOSEALL for remaining...", end=" ")
        ok, detail = cmd.close_all()
        print(f"{'OK' if ok else 'FAILED'} - {detail}")


def main() -> int:
    print("Closing ALL positions in ALL accounts...")
    close_all_accounts()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())