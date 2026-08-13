# Q2829: lending_account_deposit: frozen or disabled account still reaches value-moving code [an-account-near-health-boundaries] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_deposit` through `lending_account_deposit` with an account near health boundaries where deposit changes mode eligibility so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `deposit must only credit the caller for actual value received into the correct bank/vault context` and causing `Critical: phantom asset credit enabling theft or unbacked borrowing`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/deposit.rs` / `lending_account_deposit`
- Entrypoint: `lending_account_deposit`
- Attacker controls: an account near health boundaries where deposit changes mode eligibility
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: deposit must only credit the caller for actual value received into the correct bank/vault context
- Expected Immunefi impact: Critical: phantom asset credit enabling theft or unbacked borrowing
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
