# Q365: change_asset_shares: rounding boundary creates extractable dust [a-repeated-withdraw-redeposit-cycle] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_withdraw` with a repeated withdraw/redeposit cycle around the same small amount to push `change_asset_shares` across a rounding edge where protocol totals and user shares no longer match, breaking `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and eventually causing `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a repeated withdraw/redeposit cycle around the same small amount
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
