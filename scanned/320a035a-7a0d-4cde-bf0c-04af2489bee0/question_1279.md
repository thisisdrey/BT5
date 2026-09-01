# Q1279: contract - storage_unregister / storage_withdraw drops a non-zero balance (3)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a race against an honest party's deployment or initialisation call, call `ft_withdraw` in `contracts/poa/token/src/contract.rs` to unregister an account that still holds tokens, so the balance is destroyed or becomes claimable by another party, breaking the invariant `unregistering an account either refuses while a balance exists, or returns that balance to its owner` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `ft_withdraw` (cross-check `ft_resolve_transfer` in the same file)
- Entrypoint: a race against an honest party's deployment or initialisation call
- Attacker controls: the timing and the arguments of the competing call
- Exploit idea: NEP-145 unregister must refuse or force-close explicitly; probe the `force` flag and the balance check. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: unregistering an account either refuses while a balance exists, or returns that balance to its owner
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Register, receive tokens, then `storage_unregister`; assert the balance is not silently lost.
