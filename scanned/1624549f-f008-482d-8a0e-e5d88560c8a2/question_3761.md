# Q3761: lending_account_end_flashloan: flashloan repayment check can be satisfied with the wrong assets [a-tail-instruction-that-is] [tx-shape]

## Question
Can an unprivileged attacker use `lending_account_end_flashloan` with a tail instruction that is shape-compatible but economically wrong so `lending_account_end_flashloan` accepts flashloan repayment from the wrong asset, account, or accounting view, breaking `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction that is shape-compatible but economically wrong
- Exploit idea: Probe whether session-end logic binds repayment to the exact bank/vault/value that was borrowed rather than a superficially valid substitute. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Borrow one asset/context, attempt controlled substitution at close, and assert the session reverts unless exact economic repayment occurred. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
