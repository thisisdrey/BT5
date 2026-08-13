# Q3636: lending_account_start_flashloan: flashloan repayment check can be satisfied with the wrong assets [a-bundle-that-mixes-flashloan] [repay-domain]

## Question
Can an unprivileged attacker use `lending_account_start_flashloan` with a bundle that mixes flashloan start with order or liquidation setup so `lending_account_start_flashloan` accepts flashloan repayment from the wrong asset, account, or accounting view, breaking `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a bundle that mixes flashloan start with order or liquidation setup
- Exploit idea: Probe whether session-end logic binds repayment to the exact bank/vault/value that was borrowed rather than a superficially valid substitute. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Borrow one asset/context, attempt controlled substitution at close, and assert the session reverts unless exact economic repayment occurred. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
