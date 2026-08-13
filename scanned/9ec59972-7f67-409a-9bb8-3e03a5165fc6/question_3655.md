# Q3655: lending_account_start_flashloan: flashloan flag can be cleared after partial state mutation [a-replay-of-a-previously] [tx-shape]

## Question
Can an unprivileged attacker make `lending_account_start_flashloan` drive `lending_account_start_flashloan` with a replay of a previously valid session layout on a new account so flashloan flags clear even though the temporary privileged state was not fully resolved, violating `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a replay of a previously valid session layout on a new account
- Exploit idea: Look for paths where the session marker is removed despite unresolved debt, side effects, or reverted intermediate assumptions. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Inject failures and edge states around the end phase and assert the session flag cannot clear unless all loan invariants pass. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
