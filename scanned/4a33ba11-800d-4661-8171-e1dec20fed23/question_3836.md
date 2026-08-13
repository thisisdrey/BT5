# Q3836: lending_account_end_flashloan: flashloan state leaks into a follow-on user action [mixed-order-liquidation-instructions-around] [repay-domain]

## Question
Can an unprivileged attacker arrange mixed order/liquidation instructions around the end phase so `lending_account_end_flashloan` leaves enough flashloan session state behind that a subsequent public action through `lending_account_end_flashloan` violates `ending a flashloan must require exact repayment and exact session linkage to the corresponding start` and causes `Critical: unresolved debt, fee bypass, or unauthorized value extraction`? Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_end_flashloan`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: mixed order/liquidation instructions around the end phase
- Exploit idea: Look for stale flags, counters, or cached assumptions that persist past a finished or failed flashloan and affect later instructions. Focus specifically on pre/post-transfer amount domains and whether the session can close using the wrong economic delta.
- Invariant to test: ending a flashloan must require exact repayment and exact session linkage to the corresponding start
- Expected Immunefi impact: Critical: unresolved debt, fee bypass, or unauthorized value extraction
- Fast validation: Complete or fail a flashloan under the controlled path, then immediately execute dependent instructions and assert behavior matches a fresh account. Compare principal, fee, vault delta, and user delta under the controlled path and assert close succeeds only on exact repayment.
