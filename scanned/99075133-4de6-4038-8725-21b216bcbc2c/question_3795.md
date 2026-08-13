# Q3795: lending_account_end_flashloan: mixed instruction ordering bypasses flashloan exclusivity [a-session-where-the-borrowed] [tx-shape]

## Question
Can an unprivileged attacker pack `lending_account_end_flashloan` with a session where the borrowed and repaid asset contexts differ subtly so `lending_account_end_flashloan` allows an extra instruction sequence inside a flashloan session that violates `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causes `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a session where the borrowed and repaid asset contexts differ subtly
- Exploit idea: Target any missing first/last/exclusive validation that could let the attacker interleave otherwise forbidden state transitions during the flashloan window. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Programmatically permute allowed and forbidden instructions around the session and assert only the intended canonical ordering can execute. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
