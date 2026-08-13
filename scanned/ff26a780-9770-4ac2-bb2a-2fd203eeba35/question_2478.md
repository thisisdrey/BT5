# Q2478: validate_ix_last: first-instruction guard can be bypassed with a shaped transaction [a-transaction-that-inserts-a] [economic-not-positional]

## Question
Can an unprivileged attacker shape the transaction around `lending_account_end_flashloan` with a transaction that inserts a no-op-looking same-program tail before the true end so `validate_ix_last` fails to enforce its first-instruction assumption, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a transaction that inserts a no-op-looking same-program tail before the true end
- Exploit idea: Attack instruction-sysvar parsing and discriminator binding so a privileged sequencing assumption can be broken from a public transaction bundle. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Enumerate transaction layouts around the guard and assert every layout that violates the intended first-position rule is rejected. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
