# Q434: change_asset_shares: repeatable cycle amplifies tiny accounting drift [a-withdraw-amount-just-below] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_withdraw` under a withdraw amount just below, at, and above the last-share boundary so `change_asset_shares` leaks value through a cycle that is individually small but cumulatively breaks `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and leads to `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount just below, at, and above the last-share boundary
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
