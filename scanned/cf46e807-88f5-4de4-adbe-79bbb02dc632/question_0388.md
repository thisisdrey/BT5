# Q388: change_asset_shares: state updated before the transfer outcome is final [remaining-accounts-that-present-multiple] [cycle]

## Question
Can an unprivileged attacker make `lending_account_withdraw` reach `change_asset_shares` with remaining accounts that present multiple possible vault or bank contexts such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and causing `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: remaining accounts that present multiple possible vault or bank contexts
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
