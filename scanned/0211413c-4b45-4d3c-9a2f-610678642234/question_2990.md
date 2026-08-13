# Q2990: lending_account_withdraw: frozen or disabled account still reaches value-moving code [repeated-tiny-withdraw-redeposit-cycles] [cycle]

## Question
Can an unprivileged attacker route `lending_account_withdraw` through `lending_account_withdraw` with repeated tiny withdraw/redeposit cycles so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and causing `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: repeated tiny withdraw/redeposit cycles
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
