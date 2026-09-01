# Q5197: core - postponed burn events decouple from the actual state change (3)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, exploit that `mt_transfer` in `contracts/defuse/src/contract/tokens/nep245/core.rs` defers `mt_burn` emission to the end of the transaction while balances change immediately, so an off-chain indexer or a same-receipt consumer acts on a state it cannot yet observe, breaking the invariant `the burn events emitted by a receipt == the balance reductions that receipt committed` and leading to temporary freezing of user funds?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/core.rs](contracts/defuse/src/contract/tokens/nep245/core.rs) - `mt_transfer` (cross-check `MT_RESOLVE_TRANSFER_PER_TOKEN_GAS` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: `self.runtime.postponed_burns` batches burns; probe whether a failing later step leaves emitted-but-unapplied or applied-but-unemitted events. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the burn events emitted by a receipt == the balance reductions that receipt committed
- Expected Immunefi impact: High - Temporary freezing of user funds
- Fast validation: Abort a batch after a postponed burn is queued; assert no burn event is emitted.
