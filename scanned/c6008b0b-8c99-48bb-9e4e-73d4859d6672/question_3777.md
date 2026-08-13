# Q3777: lending_account_end_flashloan: flashloan flag can be cleared after partial state mutation [a-tail-instruction-that-is] [tx-shape]

## Question
Can an unprivileged attacker make `lending_account_end_flashloan` drive `lending_account_end_flashloan` with a tail instruction that is shape-compatible but economically wrong so flashloan flags clear even though the temporary privileged state was not fully resolved, violating `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction that is shape-compatible but economically wrong
- Exploit idea: Look for paths where the session marker is removed despite unresolved debt, side effects, or reverted intermediate assumptions. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Inject failures and edge states around the end phase and assert the session flag cannot clear unless all loan invariants pass. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
