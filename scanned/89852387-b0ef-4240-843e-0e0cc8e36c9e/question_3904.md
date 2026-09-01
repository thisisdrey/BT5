# Q3904: contract - storage_unregister / storage_withdraw drops a non-zero balance (8)

## Question
Given an honest deployment for the same derived id is already in flight, can an unprivileged attacker, entering through a token the attacker issued and then had the Verifier custody, call `ft_balance_of` in `contracts/poa/token/src/contract.rs` to unregister an account that still holds tokens, so the balance is destroyed or becomes claimable by another party, breaking the invariant `unregistering an account either refuses while a balance exists, or returns that balance to its owner` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/poa/token/src/contract.rs](contracts/poa/token/src/contract.rs) - `ft_balance_of` (cross-check `ft_total_supply` in the same file)
- Entrypoint: a token the attacker issued and then had the Verifier custody
- Attacker controls: the token's behaviour on transfer, refund and metadata reads
- Exploit idea: NEP-145 unregister must refuse or force-close explicitly; probe the `force` flag and the balance check. Set-up: an honest deployment for the same derived id is already in flight.
- Invariant to test: unregistering an account either refuses while a balance exists, or returns that balance to its owner
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Register, receive tokens, then `storage_unregister`; assert the balance is not silently lost.
