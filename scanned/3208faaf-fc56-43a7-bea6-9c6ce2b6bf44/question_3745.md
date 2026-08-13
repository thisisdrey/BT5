# Q3745: lending_account_end_flashloan: flashloan session can be opened without a safe closing shape [a-tail-instruction-that-is] [tx-shape]

## Question
Can an unprivileged attacker call `lending_account_end_flashloan` with a tail instruction that is shape-compatible but economically wrong so `lending_account_end_flashloan` starts a flashloan session that cannot be safely closed, violating `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causing `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction that is shape-compatible but economically wrong
- Exploit idea: Attack assumptions about instruction list shape, end instruction identity, and per-session flags so the protocol enters a privileged temporary state unsafely. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Construct adversarial instruction lists and assert start always rejects if a valid end phase cannot be proven up front. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
