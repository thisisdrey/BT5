# Q3650: lending_account_start_flashloan: flashloan flag can be cleared after partial state mutation [a-transaction-where-the-flashloan] [repay-domain]

## Question
Can an unprivileged attacker make `lending_account_start_flashloan` drive `lending_account_start_flashloan` with a transaction where the flashloan start is preceded by a same-program sibling instruction so flashloan flags clear even though the temporary privileged state was not fully resolved, violating `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a transaction where the flashloan start is preceded by a same-program sibling instruction
- Exploit idea: Look for paths where the session marker is removed despite unresolved debt, side effects, or reverted intermediate assumptions. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Inject failures and edge states around the end phase and assert the session flag cannot clear unless all loan invariants pass. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
