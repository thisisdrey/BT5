# Q2977: lending_account_withdraw: frozen or disabled account still reaches value-moving code [a-withdraw-amount-at-the] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_withdraw` through `lending_account_withdraw` with a withdraw amount at the last-share boundary so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and causing `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw amount at the last-share boundary
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
