# Q0954: lib - ft_resolve_transfer refund exceeds what was sent

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a receiver or callee contract the attacker deployed, invoked during a transfer callback, return a crafted value from a receiver so `PoaFactory` in `contracts/poa/factory/src/lib.rs` refunds more than the original transfer, minting supply on the token contract the Verifier custodies, breaking the invariant `refund <= amount transferred, and total supply is unchanged by a transfer-and-refund cycle` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/poa/factory/src/lib.rs](contracts/poa/factory/src/lib.rs) - `PoaFactory` (cross-check `tokens` in the same file)
- Entrypoint: a receiver or callee contract the attacker deployed, invoked during a transfer callback
- Attacker controls: the callee's return value, panics, and gas consumption
- Exploit idea: The NEP-141 resolve pattern clamps the returned 'unused' amount; probe every path where the clamp is missing or applied to the wrong operand. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: refund <= amount transferred, and total supply is unchanged by a transfer-and-refund cycle
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Return `u128::MAX` as unused from a receiver; assert the refund is clamped.
