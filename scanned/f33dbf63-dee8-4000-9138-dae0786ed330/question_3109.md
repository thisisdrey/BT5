# Q3109: nep413 - verifying_contract binding bypass (4)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), reuse a payload signed for a different deployment by making `Nep413DefuseMessage` in `contracts/defuse/core/src/payload/nep413.rs` produce a `verifying_contract` that equals `env::current_account_id()` on the target while the signer believed it was signing for another contract, breaking the invariant `the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/nep413.rs](contracts/defuse/core/src/payload/nep413.rs) - `Nep413DefuseMessage` (cross-check `extract_defuse_payload` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Attack the `recipient.parse()` / domain-separator step: casing, trailing dot, unicode, or an envelope field the signer's wallet renders differently from what the contract compares. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Assert `extract_defuse_payload()` rejects every `recipient` string that is not byte-identical to the deployed account id.
