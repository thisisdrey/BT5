# Q2565: validate_ix_last: instruction loader validates the wrong sysvar contents [duplicate-account-metas-that-change] [hash-replay]

## Question
Can an unprivileged attacker route `lending_account_end_flashloan` through `validate_ix_last` with duplicate account metas that change the meaning of the tail instruction so instruction-loader parsing validates the wrong sysvar contents, breaking `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas that change the meaning of the tail instruction
- Exploit idea: Check assumptions about instruction sysvar ordering, account indexes, and discriminators used to prove phase coupling. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Build synthetic transactions targeting parser edge cases and assert the loader either rejects or resolves the exact intended instructions only. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
