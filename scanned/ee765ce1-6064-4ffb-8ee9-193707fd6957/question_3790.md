# Q3790: lending_account_end_flashloan: flashloan flag can be cleared after partial state mutation [fee-and-amount-boundaries-near] [repay-domain]

## Question
Can an unprivileged attacker make `lending_account_end_flashloan` drive `lending_account_end_flashloan` with fee and amount boundaries near one-unit differences so flashloan flags clear even though the temporary privileged state was not fully resolved, violating `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: fee and amount boundaries near one-unit differences
- Exploit idea: Look for paths where the session marker is removed despite unresolved debt, side effects, or reverted intermediate assumptions. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Inject failures and edge states around the end phase and assert the session flag cannot clear unless all loan invariants pass. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
