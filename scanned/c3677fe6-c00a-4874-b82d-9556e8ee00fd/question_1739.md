# Q1739: error - AuthResolver resolution accepted for the wrong signer

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, make `ContractError` in `contracts/wallet/src/error.rs` return an `AuthorizationResolution` that authorises a `signer_id` other than the one the resolved access key or contract actually belongs to, breaking the invariant `the `signer_id` an `AuthorizationResolution` authorises == the account whose key was actually resolved` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/error.rs](contracts/wallet/src/error.rs) - `ContractError`
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: NEP-641 resolution maps an account/key to an authorisation decision; probe caching, stale key sets, and default-allow branches. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the `signer_id` an `AuthorizationResolution` authorises == the account whose key was actually resolved
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Resolve against a rotated/removed key; assert authorisation is refused.
