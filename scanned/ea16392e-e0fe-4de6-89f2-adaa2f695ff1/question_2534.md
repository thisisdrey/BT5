# Q2534: validate_ix_last: instruction-hash binding is replayable across contexts [duplicate-account-metas-that-change] [economic-not-positional]

## Question
Can an unprivileged attacker replay `lending_account_end_flashloan` with duplicate account metas that change the meaning of the tail instruction so `validate_ix_last` accepts an instruction-hash binding from the wrong context, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas that change the meaning of the tail instruction
- Exploit idea: Check that any hash/discriminator used to tie phases together is domain-separated by accounts, signer, and phase-specific state. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Attempt cross-context replay with the same hash material and assert it cannot satisfy the guard for another account or phase. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
