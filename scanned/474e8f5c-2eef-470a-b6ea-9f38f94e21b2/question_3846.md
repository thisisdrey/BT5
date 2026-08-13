# Q3846: lending_account_end_flashloan: flashloan close accepts a manipulated pre/post snapshot [duplicate-account-metas-across-start] [repay-domain]

## Question
Can an unprivileged attacker call `lending_account_end_flashloan` with duplicate account metas across start and end phases so `lending_account_end_flashloan` validates the flashloan from a manipulable snapshot rather than the true post-state, breaking `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas across start and end phases
- Exploit idea: Probe whether balance deltas can be hidden behind same-slot transfers, ATA substitutions, or reused accounts between start and end. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Instrument snapshots before and after the controlled path and assert every accepted close matches the actual economic delta for the right vault. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
