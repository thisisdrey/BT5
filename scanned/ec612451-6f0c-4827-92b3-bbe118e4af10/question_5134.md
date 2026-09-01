# Q5134: withdraw - attacker-controlled promise result inflates the refund (25)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, return a crafted value from an attacker-deployed token or receiver contract so `nft_resolve_withdraw` in `contracts/defuse/src/contract/tokens/nep171/withdraw.rs` re-credits more than the amount that actually failed to settle, breaking the invariant `value debited == value actually delivered + value re-credited by the resolver` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/withdraw.rs](contracts/defuse/src/contract/tokens/nep171/withdraw.rs) - `nft_resolve_withdraw` (cross-check `NFT_RESOLVE_WITHDRAW_GAS` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: The refund is parsed from the callee's return value with `promise_result_checked_json*`; drive the parsed value above the amount actually retained by the callee, or make the clamp (`min(amount)`, `min(balance_left)`) select the wrong bound. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: value debited == value actually delivered + value re-credited by the resolver
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy a token whose `ft_transfer_call` transfers everything but returns `0`; assert the balance is not re-credited.
