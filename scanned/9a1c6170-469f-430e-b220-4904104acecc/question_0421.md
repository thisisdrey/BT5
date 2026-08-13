# Q421: change_asset_shares: frozen or disabled account still reaches value-moving code [a-withdraw-immediately-after-a] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_withdraw` through `change_asset_shares` with a withdraw immediately after a tiny deposit that leaves dust-sized active shares so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns` and causing `Critical: direct theft or unauthorized withdrawal of protocol/user funds`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_asset_shares`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw immediately after a tiny deposit that leaves dust-sized active shares
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: asset-share reductions must exactly match released value and cannot free more liquidity than the user economically owns
- Expected Immunefi impact: Critical: direct theft or unauthorized withdrawal of protocol/user funds
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
