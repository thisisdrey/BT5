# Q2481: validate_ix_last: last-instruction guard accepts a semantically wrong tail [a-transaction-with-an-economically] [hash-replay]

## Question
Can an unprivileged attacker build `lending_account_end_flashloan` with a transaction with an economically different but shape-compatible tail instruction so `validate_ix_last` accepts a semantically wrong last instruction, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and leading to `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a transaction with an economically different but shape-compatible tail instruction
- Exploit idea: Check whether the guard validates only position or discriminator fragments, not the full action and accounts that the final phase assumes. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Substitute tail instructions with matching-looking shapes and assert the guard still rejects every non-canonical close/finalization path. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
