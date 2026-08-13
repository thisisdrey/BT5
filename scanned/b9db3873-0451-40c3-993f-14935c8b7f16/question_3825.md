# Q3825: lending_account_end_flashloan: flashloan state leaks into a follow-on user action [a-tail-instruction-that-is] [tx-shape]

## Question
Can an unprivileged attacker arrange a tail instruction that is shape-compatible but economically wrong so `lending_account_end_flashloan` leaves enough flashloan session state behind that a subsequent public action through `lending_account_end_flashloan` violates `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causes `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction that is shape-compatible but economically wrong
- Exploit idea: Look for stale flags, counters, or cached assumptions that persist past a finished or failed flashloan and affect later instructions. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Complete or fail a flashloan under the controlled path, then immediately execute dependent instructions and assert behavior matches a fresh account. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
