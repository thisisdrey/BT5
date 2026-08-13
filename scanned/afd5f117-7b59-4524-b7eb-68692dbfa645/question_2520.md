# Q2520: validate_ix_last: program-allowlist guard can be confused by a crafted CPI shape [a-tail-instruction-with-the] [economic-not-positional]

## Question
Can an unprivileged attacker use `lending_account_end_flashloan` with a tail instruction with the same discriminator family but wrong accounts so `validate_ix_last` treats a crafted instruction or CPI context as allowed when it should not be, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a tail instruction with the same discriminator family but wrong accounts
- Exploit idea: Probe any hashing, discriminator, or program-id checks that assume a simpler instruction structure than a hostile caller can supply. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Craft edge-case instruction data/account metas and assert the allowlist logic cannot be tricked into green-lighting a forbidden context. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
