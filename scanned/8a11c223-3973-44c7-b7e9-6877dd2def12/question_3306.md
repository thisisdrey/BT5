# Q3306: lending_account_repay: frozen or disabled account still reaches value-moving code [tiny-repeated-repay-amounts-intended] [cycle]

## Question
Can an unprivileged attacker route `lending_account_repay` through `lending_account_repay` with tiny repeated repay amounts intended to ratchet debt downward asymmetrically so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and causing `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: tiny repeated repay amounts intended to ratchet debt downward asymmetrically
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
