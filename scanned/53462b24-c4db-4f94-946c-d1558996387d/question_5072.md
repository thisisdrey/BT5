# Q5072: withdraw - attacker-controlled promise result inflates the refund (24)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, return a crafted value from an attacker-deployed token or receiver contract so `NFT_RESOLVE_WITHDRAW_GAS` in `contracts/defuse/src/contract/tokens/nep171/withdraw.rs` re-credits more than the amount that actually failed to settle, breaking the invariant `value debited == value actually delivered + value re-credited by the resolver` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/withdraw.rs](contracts/defuse/src/contract/tokens/nep171/withdraw.rs) - `NFT_RESOLVE_WITHDRAW_GAS` (cross-check `DO_NFT_WITHDRAW_GAS` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: The refund is parsed from the callee's return value with `promise_result_checked_json*`; drive the parsed value above the amount actually retained by the callee, or make the clamp (`min(amount)`, `min(balance_left)`) select the wrong bound. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: value debited == value actually delivered + value re-credited by the resolver
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy a token whose `ft_transfer_call` transfers everything but returns `0`; assert the balance is not re-credited.
