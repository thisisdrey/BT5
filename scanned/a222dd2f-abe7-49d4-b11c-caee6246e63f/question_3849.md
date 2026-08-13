# Q3849: lending_account_end_flashloan: flashloan close accepts a manipulated pre/post snapshot [a-same-slot-transfer-pattern] [tx-shape]

## Question
Can an unprivileged attacker call `lending_account_end_flashloan` with a same-slot transfer pattern intended to spoof net repayment so `lending_account_end_flashloan` validates the flashloan from a manipulable snapshot rather than the true post-state, breaking `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a same-slot transfer pattern intended to spoof net repayment
- Exploit idea: Probe whether balance deltas can be hidden behind same-slot transfers, ATA substitutions, or reused accounts between start and end. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Instrument snapshots before and after the controlled path and assert every accepted close matches the actual economic delta for the right vault. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
