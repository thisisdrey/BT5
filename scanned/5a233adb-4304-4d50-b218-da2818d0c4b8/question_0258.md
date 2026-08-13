# Q258: get_liability_shares: frozen or disabled account still reaches value-moving code [a-borrow-amount-at-the] [cycle]

## Question
Can an unprivileged attacker route `lending_account_borrow` through `get_liability_shares` with a borrow amount at the smallest non-zero liability-share boundary so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow amount at the smallest non-zero liability-share boundary
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
