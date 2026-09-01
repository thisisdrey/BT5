# Q1927: state - approval/callback re-entrancy on the peripheral token

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a receiver or callee contract the attacker deployed, invoked during a transfer callback, have a receiver contract re-enter `with_code` in `contracts/outlayer/app/src/state.rs` during a transfer callback so balances are read or written between the debit and the resolve, breaking the invariant `balances observed by a re-entrant call == balances after the in-flight transfer settles or reverts` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/outlayer/app/src/state.rs](contracts/outlayer/app/src/state.rs) - `with_code` (cross-check `with_code_hash` in the same file)
- Entrypoint: a receiver or callee contract the attacker deployed, invoked during a transfer callback
- Attacker controls: the callee's return value, panics, and gas consumption
- Exploit idea: The peripheral token is what the Verifier custodies; re-entrancy here inflates what the Verifier believes it holds. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: balances observed by a re-entrant call == balances after the in-flight transfer settles or reverts
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Deploy a re-entrant receiver; assert no double credit.
