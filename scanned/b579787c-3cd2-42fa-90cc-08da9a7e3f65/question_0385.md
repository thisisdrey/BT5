# Q385: change_asset_shares: state updated before the transfer outcome is final [a-withdraw-amount-just-below] [cache-order]

## Question
Can an unprivileged attacker make `lending_account_withdraw` reach `change_asset_shares` with a withdraw amount just below, at, and above the last-share boundary such that accounting mutates before the real token/value transfer is conclusively enforced, breaking `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and causing `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount just below, at, and above the last-share boundary
- Exploit idea: Check whether partial state mutation can survive a later transfer/accounting edge and leave the user with value or debt inconsistent with actual token movement. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Inject the controlled token/account conditions and assert that any downstream failure rolls back all shares, caches, and flags atomically. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
