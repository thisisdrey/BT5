# Q4237: imt - postponed burn events decouple from the actual state change (44)

## Question
Given `min_gas` is set to exactly the documented minimum for that path, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, exploit that `imt_burn` in `contracts/defuse/src/contract/tokens/imt.rs` defers `mt_burn` emission to the end of the transaction while balances change immediately, so an off-chain indexer or a same-receipt consumer acts on a state it cannot yet observe, breaking the invariant `the burn events emitted by a receipt == the balance reductions that receipt committed` and leading to temporary freezing of user funds?

## Target
- File/function: [contracts/defuse/src/contract/tokens/imt.rs](contracts/defuse/src/contract/tokens/imt.rs) - `imt_burn`
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: `self.runtime.postponed_burns` batches burns; probe whether a failing later step leaves emitted-but-unapplied or applied-but-unemitted events. Set-up: `min_gas` is set to exactly the documented minimum for that path.
- Invariant to test: the burn events emitted by a receipt == the balance reductions that receipt committed
- Expected Immunefi impact: High - Temporary freezing of user funds
- Fast validation: Abort a batch after a postponed burn is queued; assert no burn event is emitted.
