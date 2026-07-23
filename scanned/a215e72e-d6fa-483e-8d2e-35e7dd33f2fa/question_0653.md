# Q653: Cross-Module Validation Gap in update_operator_with_same_commission

## Question
Can `aptos_framework::vesting::update_operator_with_same_commission` trust a value, ref, or helper result from another module without rechecking the exact assumptions needed to protect ownership, amount, or state-transition safety?

## Target
- File/function: aptos-move/framework/aptos-framework/sources/vesting.move::aptos_framework::vesting::update_operator_with_same_commission
- Entrypoint: Submit an entry-function transaction invoking `aptos_framework::vesting::update_operator_with_same_commission` or the direct user flow that reaches it.
- Attacker controls: stake amounts, lockup windows, delegated voter or operator choices, beneficiaries, commission-related inputs, and recipient addresses
- Exploit idea: Exploit any gap between a borrowed cross-module assumption and the stronger invariant this entry function actually needs.
- Invariant to test: Every externally sourced value or ref used by the entry function must be revalidated against the safety invariant it protects here.
- Expected Immunefi impact: High. Acceptance of forged, stale, malformed, differently encoded, insufficiently bound, or context-confused bytecode, signatures, keyless proofs, JWK material, module metadata, write sets, or state proofs that bypass mainnet execution or verification rules.
- Fast validation: Write a Move test that supplies edge-case cross-module values and assert the entry function rechecks all assumptions before mutating state.
