# Q2549: validate_ix_last: stack-height or not-CPI guard misses a reachable public path [duplicate-account-metas-that-change] [hash-replay]

## Question
Can an unprivileged attacker call `lending_account_end_flashloan` with duplicate account metas that change the meaning of the tail instruction so `validate_ix_last` misses a reachable CPI/stack-height edge and executes in a forbidden context, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas that change the meaning of the tail instruction
- Exploit idea: Audit whether CPI or stack-height restrictions are enforced uniformly across every sensitive path that assumes direct invocation. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Invoke the path from allowed and adversarial calling contexts and assert the guard rejects every forbidden invocation pattern. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
