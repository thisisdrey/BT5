# Q1586: event - postponed burn events decouple from the actual state change (8)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, exploit that `REFUND_MEMO` in `crates/near/utils/src/event.rs` defers `mt_burn` emission to the end of the transaction while balances change immediately, so an off-chain indexer or a same-receipt consumer acts on a state it cannot yet observe, breaking the invariant `the burn events emitted by a receipt == the balance reductions that receipt committed` and leading to temporary freezing of user funds?

## Target
- File/function: [crates/near/utils/src/event.rs](crates/near/utils/src/event.rs) - `REFUND_MEMO`
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: `self.runtime.postponed_burns` batches burns; probe whether a failing later step leaves emitted-but-unapplied or applied-but-unemitted events. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the burn events emitted by a receipt == the balance reductions that receipt committed
- Expected Immunefi impact: High - Temporary freezing of user funds
- Fast validation: Abort a batch after a postponed burn is queued; assert no burn event is emitted.
