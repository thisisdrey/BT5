# Q2486: validate_ix_last: last-instruction guard accepts a semantically wrong tail [duplicate-account-metas-that-change] [economic-not-positional]

## Question
Can an unprivileged attacker build `lending_account_end_flashloan` with duplicate account metas that change the meaning of the tail instruction so `validate_ix_last` accepts a semantically wrong last instruction, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and leading to `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas that change the meaning of the tail instruction
- Exploit idea: Check whether the guard validates only position or discriminator fragments, not the full action and accounts that the final phase assumes. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Substitute tail instructions with matching-looking shapes and assert the guard still rejects every non-canonical close/finalization path. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
