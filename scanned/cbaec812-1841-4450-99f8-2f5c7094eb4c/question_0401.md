# Q401: change_asset_shares: cross-mode collateral view mismatch [a-withdraw-amount-just-below] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a withdraw amount just below, at, and above the last-share boundary so `change_asset_shares` evaluates account risk under one mode and settles value under another, violating `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and resulting in `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount just below, at, and above the last-share boundary
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
