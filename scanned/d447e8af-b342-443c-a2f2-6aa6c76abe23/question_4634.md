# Q4634: contract - attached deposit accounting on w_execute_signed (3)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, exploit that `execute_extension` in `contracts/wallet/src/contract.rs` accepts any attached deposit (or requires a non-zero one) so a relayer's or the wallet's NEAR is consumed by an action the owner did not authorise, breaking the invariant `NEAR spent by a wallet execution == NEAR the owner's signed request authorises` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/contract.rs](contracts/wallet/src/contract.rs) - `execute_extension` (cross-check `w_resolve_auth` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: `pay_for_gas` is documented as currently unsupported; probe whether the flag or the deposit changes execution. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: NEAR spent by a wallet execution == NEAR the owner's signed request authorises
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Submit a signed request with a large attached deposit and `pay_for_gas` set; assert the documented behaviour.
