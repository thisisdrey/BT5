# Q2582: validate_ix_last: guarded multi-phase flow accepts extra side effects in between [duplicate-account-metas-that-change] [economic-not-positional]

## Question
Can an unprivileged attacker combine `lending_account_end_flashloan` with duplicate account metas that change the meaning of the tail instruction so `validate_ix_last` allows an extra side effect between guarded phases, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and leading to `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: duplicate account metas that change the meaning of the tail instruction
- Exploit idea: Attack any assumption that no other user-accessible state transition can occur between coupled phases once guards pass. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Insert candidate side-effect instructions between phases and assert the coupled flow rejects unless the critical section is truly exclusive. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
