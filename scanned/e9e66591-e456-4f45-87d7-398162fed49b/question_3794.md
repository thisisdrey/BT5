# Q3794: lending_account_end_flashloan: mixed instruction ordering bypasses flashloan exclusivity [a-tail-instruction-that-is] [repay-domain]

## Question
Can an unprivileged attacker pack `lending_account_end_flashloan` with a tail instruction that is shape-compatible but economically wrong so `lending_account_end_flashloan` allows an extra instruction sequence inside a flashloan session that violates `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causes `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction that is shape-compatible but economically wrong
- Exploit idea: Target any missing first/last/exclusive validation that could let the attacker interleave otherwise forbidden state transitions during the flashloan window. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Programmatically permute allowed and forbidden instructions around the session and assert only the intended canonical ordering can execute. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
