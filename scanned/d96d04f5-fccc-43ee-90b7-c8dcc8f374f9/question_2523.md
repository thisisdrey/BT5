# Q2523: validate_ix_last: program-allowlist guard can be confused by a crafted CPI shape [a-replay-of-a-previously] [hash-replay]

## Question
Can an unprivileged attacker use `lending_account_end_flashloan` with a replay of a previously valid end-phase layout under a new context so `validate_ix_last` treats a crafted instruction or CPI context as allowed when it should not be, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a replay of a previously valid end-phase layout under a new context
- Exploit idea: Probe any hashing, discriminator, or program-id checks that assume a simpler instruction structure than a hostile caller can supply. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Craft edge-case instruction data/account metas and assert the allowlist logic cannot be tricked into green-lighting a forbidden context. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
