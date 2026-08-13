# Q2475: validate_ix_last: first-instruction guard can be bypassed with a shaped transaction [a-replay-of-a-previously] [hash-replay]

## Question
Can an unprivileged attacker shape the transaction around `lending_account_end_flashloan` with a replay of a previously valid end-phase layout under a new context so `validate_ix_last` fails to enforce its first-instruction assumption, violating `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a replay of a previously valid end-phase layout under a new context
- Exploit idea: Attack instruction-sysvar parsing and discriminator binding so a privileged sequencing assumption can be broken from a public transaction bundle. Focus specifically on replay of previously valid instruction hashes, layouts, or discriminator families across new contexts.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Enumerate transaction layouts around the guard and assert every layout that violates the intended first-position rule is rejected. Reuse previously valid instruction layouts against new accounts and assert the guard cannot be satisfied cross-context.
