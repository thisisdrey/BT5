# Q3729: lending_account_start_flashloan: session reuse or replay after a valid close [a-transaction-where-the-flashloan] [tx-shape]

## Question
Can an unprivileged attacker replay or reuse `lending_account_start_flashloan` with a transaction where the flashloan start is preceded by a same-program sibling instruction so `lending_account_start_flashloan` treats an old flashloan session as still valid, violating `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and leading to `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a transaction where the flashloan start is preceded by a same-program sibling instruction
- Exploit idea: Check whether hashes, discriminators, or instruction bindings used to link start and end are one-time and domain-separated. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Attempt to replay the exact end conditions after one successful close and assert all subsequent reuse attempts fail. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
