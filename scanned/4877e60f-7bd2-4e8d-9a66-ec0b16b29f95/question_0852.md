# Q0852: imt - postponed burn events decouple from the actual state change (9)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, exploit that `imt_burn` in `contracts/defuse/src/contract/tokens/imt.rs` defers `mt_burn` emission to the end of the transaction while balances change immediately, so an off-chain indexer or a same-receipt consumer acts on a state it cannot yet observe, breaking the invariant `the burn events emitted by a receipt == the balance reductions that receipt committed` and leading to temporary freezing of user funds?

## Target
- File/function: [contracts/defuse/src/contract/tokens/imt.rs](contracts/defuse/src/contract/tokens/imt.rs) - `imt_burn`
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: `self.runtime.postponed_burns` batches burns; probe whether a failing later step leaves emitted-but-unapplied or applied-but-unemitted events. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the burn events emitted by a receipt == the balance reductions that receipt committed
- Expected Immunefi impact: High - Temporary freezing of user funds
- Fast validation: Abort a batch after a postponed burn is queued; assert no burn event is emitted.
