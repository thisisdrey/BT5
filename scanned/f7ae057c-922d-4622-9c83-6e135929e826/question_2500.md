# Q2500: validate_ix_last: exclusive instruction set can be bypassed by account-equivalent calls [a-transaction-that-appends-public] [economic-not-positional]

## Question
Can an unprivileged attacker combine `lending_account_end_flashloan` with a transaction that appends public sibling instructions after the supposed end phase so `validate_ix_last` misses an economically equivalent forbidden instruction, breaking `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a transaction that appends public sibling instructions after the supposed end phase
- Exploit idea: Look for exclusivity checks that enumerate exact variants but may miss a sibling path that changes the same state in the same critical section. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Bundle all economically equivalent candidate instructions with the guarded entrypoint and assert they are rejected consistently. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
