# Q3707: lending_account_start_flashloan: flashloan state leaks into a follow-on user action [a-transaction-that-inserts-a] [tx-shape]

## Question
Can an unprivileged attacker arrange a transaction that inserts a helper or sync instruction before the end phase so `lending_account_start_flashloan` leaves enough flashloan session state behind that a subsequent public action through `lending_account_start_flashloan` violates `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causes `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a transaction that inserts a helper or sync instruction before the end phase
- Exploit idea: Look for stale flags, counters, or cached assumptions that persist past a finished or failed flashloan and affect later instructions. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Complete or fail a flashloan under the controlled path, then immediately execute dependent instructions and assert behavior matches a fresh account. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
