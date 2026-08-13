# Q2754: lending_account_deposit: rounding boundary creates extractable dust [a-deposit-amount-at-tiny] [cycle]

## Question
Can an unprivileged attacker use `lending_account_deposit` with a deposit amount at tiny, threshold, and one-share boundaries to push `lending_account_deposit` across a rounding edge where protocol totals and user shares no longer match, breaking `deposit must only credit the caller for actual value received into the correct bank/vault context` and eventually causing `Critical: phantom asset credit enabling theft or unbacked borrowing`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/deposit.rs` / `lending_account_deposit`
- Entrypoint: `lending_account_deposit`
- Attacker controls: a deposit amount at tiny, threshold, and one-share boundaries
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: deposit must only credit the caller for actual value received into the correct bank/vault context
- Expected Immunefi impact: Critical: phantom asset credit enabling theft or unbacked borrowing
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
