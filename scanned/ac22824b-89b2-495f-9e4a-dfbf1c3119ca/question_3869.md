# Q3869: lending_account_end_flashloan: session reuse or replay after a valid close [fee-and-amount-boundaries-near] [tx-shape]

## Question
Can an unprivileged attacker replay or reuse `lending_account_end_flashloan` with fee and amount boundaries near one-unit differences so `lending_account_end_flashloan` treats an old flashloan session as still valid, violating `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and leading to `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: fee and amount boundaries near one-unit differences
- Exploit idea: Check whether hashes, discriminators, or instruction bindings used to link start and end are one-time and domain-separated. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Attempt to replay the exact end conditions after one successful close and assert all subsequent reuse attempts fail. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
