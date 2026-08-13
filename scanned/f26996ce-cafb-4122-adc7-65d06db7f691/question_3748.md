# Q3748: lending_account_end_flashloan: flashloan session can be opened without a safe closing shape [a-session-where-the-borrowed] [repay-domain]

## Question
Can an unprivileged attacker call `lending_account_end_flashloan` with a session where the borrowed and repaid asset contexts differ subtly so `lending_account_end_flashloan` starts a flashloan session that cannot be safely closed, violating `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a session where the borrowed and repaid asset contexts differ subtly
- Exploit idea: Attack assumptions about instruction list shape, end instruction identity, and per-session flags so the protocol enters a privileged temporary state unsafely. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Construct adversarial instruction lists and assert start always rejects if a valid end phase cannot be proven up front. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
