# Q3635: lending_account_start_flashloan: flashloan repayment check can be satisfied with the wrong assets [a-bundle-that-mixes-flashloan] [tx-shape]

## Question
Can an unprivileged attacker use `lending_account_start_flashloan` with a bundle that mixes flashloan start with order or liquidation setup so `lending_account_start_flashloan` accepts flashloan repayment from the wrong asset, account, or accounting view, breaking `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a bundle that mixes flashloan start with order or liquidation setup
- Exploit idea: Probe whether session-end logic binds repayment to the exact bank/vault/value that was borrowed rather than a superficially valid substitute. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Borrow one asset/context, attempt controlled substitution at close, and assert the session reverts unless exact economic repayment occurred. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
