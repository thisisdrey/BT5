# Q2844: lending_account_deposit: repeatable cycle amplifies tiny accounting drift [a-user-with-active-balances] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_deposit` under a user with active balances across multiple asset tags so `lending_account_deposit` leaks value through a cycle that is individually small but cumulatively breaks `deposit must only credit the caller for actual value received into the correct bank/vault context` and leads to `Critical: phantom asset credit enabling theft or unbacked borrowing`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/deposit.rs` / `lending_account_deposit`
- Entrypoint: `lending_account_deposit`
- Attacker controls: a user with active balances across multiple asset tags
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: deposit must only credit the caller for actual value received into the correct bank/vault context
- Expected Immunefi impact: Critical: phantom asset credit enabling theft or unbacked borrowing
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
