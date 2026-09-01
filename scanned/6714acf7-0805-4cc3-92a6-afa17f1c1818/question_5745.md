# Q5745: deposit - refund_if_fails changes atomicity in the attacker's favour (15)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, exploit the `refund_if_fails` branch reached from `nft_on_transfer` in `contracts/defuse/src/contract/tokens/nep171/deposit.rs`, which either calls `execute_intents` inline or schedules it as a detached promise, so a failure refunds the deposit while the intents still take effect, breaking the invariant `a deposit is refunded only when the intents it funded had no effect` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/deposit.rs](contracts/defuse/src/contract/tokens/nep171/deposit.rs) - `nft_on_transfer` (cross-check `nft_resolve_deposit` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: Inline execution shares the receipt; the detached `ext_intents::...detach()` path does not, so failure semantics differ between the two. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: a deposit is refunded only when the intents it funded had no effect
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox both branches with a failing intent; assert deposit and effect are all-or-nothing.
