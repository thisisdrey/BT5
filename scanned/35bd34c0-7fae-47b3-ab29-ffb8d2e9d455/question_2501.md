# Q2501: validate_ix_last: exclusive instruction set can be bypassed by account-equivalent calls [duplicate-account-metas-that-change] [hash-replay]

## Question
Can an unprivileged attacker combine `lending_account_end_flashloan` with duplicate account metas that change the meaning of the tail instruction so `validate_ix_last` misses an economically equivalent forbidden instruction, breaking `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas that change the meaning of the tail instruction
- Exploit idea: Look for exclusivity checks that enumerate exact variants but may miss a sibling path that changes the same state in the same critical section. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Bundle all economically equivalent candidate instructions with the guarded entrypoint and assert they are rejected consistently. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
