# Q2570: validate_ix_last: instruction loader validates the wrong sysvar contents [a-bundle-that-mixes-flashloan] [economic-not-positional]

## Question
Can an unprivileged attacker route `lending_account_end_flashloan` through `validate_ix_last` with a bundle that mixes flashloan close with order/liquidation finalization so instruction-loader parsing validates the wrong sysvar contents, breaking `end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution` and causing `Critical: unauthorized session close that leaves value extracted or debt unresolved`? Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.

## Target
- File/function: `programs/marginfi/src/ix_utils.rs` / `validate_ix_last`
- Entrypoint: `lending_account_end_flashloan`
- Attacker controls: a bundle that mixes flashloan close with order/liquidation finalization
- Exploit idea: Check assumptions about instruction sysvar ordering, account indexes, and discriminators used to prove phase coupling. Focus specifically on whether the guard proves economic exclusivity, not only positional ordering.
- Invariant to test: end-phase validation must prove the exact intended terminal instruction and prevent replay or substitution
- Expected Immunefi impact: Critical: unauthorized session close that leaves value extracted or debt unresolved
- Fast validation: Build synthetic transactions targeting parser edge cases and assert the loader either rejects or resolves the exact intended instructions only. Bundle same-program sibling actions that change the same balances indirectly and assert exclusivity still blocks them.
