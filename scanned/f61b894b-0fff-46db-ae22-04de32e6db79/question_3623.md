# Q3623: lending_account_start_flashloan: flashloan session can be opened without a safe closing shape [a-replay-of-a-previously] [tx-shape]

## Question
Can an unprivileged attacker call `lending_account_start_flashloan` with a replay of a previously valid session layout on a new account so `lending_account_start_flashloan` starts a flashloan session that cannot be safely closed, violating `starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved` and causing `Critical: direct theft or unbacked debt through flashloan misuse`? Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs` / `lending_account_start_flashloan`
- Entrypoint: `lending_account_start_flashloan`
- Attacker controls: a replay of a previously valid session layout on a new account
- Exploit idea: Attack assumptions about instruction list shape, end instruction identity, and per-session flags so the protocol enters a privileged temporary state unsafely. Focus specifically on adversarial transaction shape and instruction-sysvar edge cases rather than simple amount changes.
- Invariant to test: starting a flashloan must prove a safe unique session and forbid any interleaving that could leak value or leave debt unresolved
- Expected Immunefi impact: Critical: direct theft or unbacked debt through flashloan misuse
- Fast validation: Construct adversarial instruction lists and assert start always rejects if a valid end phase cannot be proven up front. Generate adversarial instruction layouts, duplicate metas, and sibling calls and assert session guards reject every non-canonical shape.
