# Q2828: lending_account_deposit: frozen or disabled account still reaches value-moving code [a-user-with-active-balances] [cycle]

## Question
Can an unprivileged attacker route `lending_account_deposit` through `lending_account_deposit` with a user with active balances across multiple asset tags so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `deposit must only credit the caller for actual value received into the correct bank/vault context` and causing `Critical: phantom asset credit enabling theft or unbacked borrowing`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/deposit.rs` / `lending_account_deposit`
- Entrypoint: `lending_account_deposit`
- Attacker controls: a user with active balances across multiple asset tags
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: deposit must only credit the caller for actual value received into the correct bank/vault context
- Expected Immunefi impact: Critical: phantom asset credit enabling theft or unbacked borrowing
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
