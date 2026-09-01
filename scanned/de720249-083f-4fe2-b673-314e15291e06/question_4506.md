# Q4506: contract - attached deposit accounting on w_execute_signed

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account, exploit that `Wallet` in `contracts/wallet/src/contract.rs` accepts any attached deposit (or requires a non-zero one) so a relayer's or the wallet's NEAR is consumed by an action the owner did not authorise, breaking the invariant `NEAR spent by a wallet execution == NEAR the owner's signed request authorises` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/contract.rs](contracts/wallet/src/contract.rs) - `Wallet` (cross-check `w_is_extension_enabled` in the same file)
- Entrypoint: `w_execute_signed(msg: RequestMessage, proof: String)` - relayable by any account
- Attacker controls: the entire `RequestMessage` (chain_id, signer_id, nonce, deadline, ops) and the `proof` string
- Exploit idea: `pay_for_gas` is documented as currently unsupported; probe whether the flag or the deposit changes execution. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: NEAR spent by a wallet execution == NEAR the owner's signed request authorises
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a signed request with a large attached deposit and `pay_for_gas` set; assert the documented behaviour.
