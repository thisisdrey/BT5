# Q86: get_asset_shares: cross-mode collateral view mismatch [a-same-transaction-sequence-that] [cycle]

## Question
Can an unprivileged attacker use `lending_account_deposit` with a same-transaction sequence that first changes account mode and then deposits so `get_asset_shares` evaluates account risk under one mode and settles value under another, violating `deposited assets must map to exactly one conservative internal share increase and never create withdrawable phantom value` and resulting in `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_asset_shares`
- Entrypoint: `lending_account_deposit`
- Attacker controls: a same-transaction sequence that first changes account mode and then deposits
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: deposited assets must map to exactly one conservative internal share increase and never create withdrawable phantom value
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
