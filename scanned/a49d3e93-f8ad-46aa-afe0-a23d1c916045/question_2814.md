# Q2814: lending_account_deposit: cross-mode collateral view mismatch [an-account-near-health-boundaries] [cycle]

## Question
Can an unprivileged attacker use `lending_account_deposit` with an account near health boundaries where deposit changes mode eligibility so `lending_account_deposit` evaluates account risk under one mode and settles value under another, violating `deposit must only credit the caller for actual value received into the correct bank/vault context` and resulting in `Critical: phantom asset credit enabling theft or unbacked borrowing`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/deposit.rs` / `lending_account_deposit`
- Entrypoint: `lending_account_deposit`
- Attacker controls: an account near health boundaries where deposit changes mode eligibility
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: deposit must only credit the caller for actual value received into the correct bank/vault context
- Expected Immunefi impact: Critical: phantom asset credit enabling theft or unbacked borrowing
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
