# Q3858: lending_account_end_flashloan: session reuse or replay after a valid close [a-tail-instruction-that-is] [repay-domain]

## Question
Can an unprivileged attacker replay or reuse `lending_account_end_flashloan` with a tail instruction that is shape-compatible but economically wrong so `lending_account_end_flashloan` treats an old flashloan session as still valid, violating `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and leading to `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction that is shape-compatible but economically wrong
- Exploit idea: Check whether hashes, discriminators, or instruction bindings used to link start and end are one-time and domain-separated. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Attempt to replay the exact end conditions after one successful close and assert all subsequent reuse attempts fail. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
