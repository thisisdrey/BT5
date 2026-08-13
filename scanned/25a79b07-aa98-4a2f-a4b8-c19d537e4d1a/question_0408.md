# Q408: change_asset_shares: cross-mode collateral view mismatch [a-user-with-multiple-active] [cycle]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a user with multiple active balances where one is reopened in the same transaction so `change_asset_shares` evaluates account risk under one mode and settles value under another, violating `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and resulting in `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a user with multiple active balances where one is reopened in the same transaction
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
