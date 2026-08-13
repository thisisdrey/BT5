# Q3618: lending_account_start_flashloan: flashloan session can be opened without a safe closing shape [a-transaction-where-the-flashloan] [repay-domain]

## Question
Can an unprivileged attacker call `lending_account_start_flashloan` with a transaction where the flashloan start is preceded by a same-program sibling instruction so `lending_account_start_flashloan` starts a flashloan session that cannot be safely closed, violating `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a transaction where the flashloan start is preceded by a same-program sibling instruction
- Exploit idea: Attack assumptions about instruction list shape, end instruction identity, and per-session flags so the protocol enters a privileged temporary state unsafely. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Construct adversarial instruction lists and assert start always rejects if a valid end phase cannot be proven up front. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
