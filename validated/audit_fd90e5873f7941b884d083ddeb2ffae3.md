## Title
Poisoned Nakamoto-block batch causes the whole (and subsequent) batches from the same relayer to be silently dropped, enabling censorship of otherwise-valid blocks - (File: `stackslib/src/net/relay.rs`)

### Summary
`Relayer::process_pushed_nakamoto_blocks` validates each `NakamotoBlocksData` batch pushed by a peer as an atomic unit before processing any of its blocks. If validation fails for *any single block* in a batch, the function `break`s out of the loop over that peer's remaining batches for the poll cycle, discarding every subsequent (fully valid) batch the same peer sent — without banning the peer or recording any fault. This mirrors the reported ERC‑20 pattern: a single "poisoned" item inside a batch of otherwise-independent, valid items causes the whole batch (and here, subsequent batches) to be rejected, at zero cost to the attacker.

### Finding Description
In [1](#0-0) . `validate_nakamoto_blocks_push` itself iterates every block in a batch and returns `Err` as soon as one block does not correspond to the expected sortition [2](#0-1) . Because `break` exits the loop over the *entire* vector `relayers_and_block_data` for that neighbor key, any batch that would have been iterated afterward — even one composed entirely of legitimate, independently-verifiable blocks relayed by that same peer — is skipped completely: it is never validated, never processed, and never added to `pushed_blocks`, so it is neither stored locally nor forwarded to other peers via `relay_epoch3_blocks`.

Crucially, unlike the individual-block `InvalidStacksBlock` failure path a few lines below (which does `bad_neighbors.push(...)` to ban the offending peer) [3](#0-2) , the batch-level `validate_nakamoto_blocks_push` failure path does **not** add the neighbor to `bad_neighbors`. The peer is never punished, so it can repeat this pattern on every poll cycle indefinitely.

An unprivileged remote peer acting as a relayer can therefore:
1. Include one block whose consensus hash does not correspond to a valid sortition, at the front of the batch list it pushes to a victim in a given cycle.
2. Follow it (in `network_result.pushed_nakamoto_blocks` for that same neighbor key) with additional, entirely valid `NakamotoBlocksData` batches (e.g., legitimately relayed blocks from other honest tenures).
3. Because of the `break`, none of the trailing valid batches get processed by the victim in that cycle, and the malicious peer suffers no consequence (no ban), so it can repeat this every cycle.

This breaks the expected invariant that "each independently-verifiable unit in a batch is validated and processed on its own merits" — instead, one broken unit vetoes co-batched, valid units, exactly as with the ERC20 "block one transfer to break the whole relayer batch" pattern.

### Impact Explanation
This is a High-severity availability/censorship issue on the read/relay path: a peer can selectively suppress delivery of legitimate Nakamoto blocks through itself to a victim node, at no cost and with no penalty, by bundling one deliberately-invalid block ahead of good ones. While the victim node may eventually receive the same blocks via its download state machine or other peers, this degrades block propagation timeliness and availability from any given relayer, and is repeatable indefinitely since the attacker is never banned for the batch-level failure (in contrast to the individual-block failure path, which does ban).

### Likelihood Explanation
Any remote, unauthenticated (from the block-processing perspective; only requires an established p2p connection with no special role) peer that can push `NakamotoBlocks` messages can trigger this. Crafting one block that fails `validate_nakamoto_blocks_push` (e.g., referencing a consensus hash/sortition mismatch) is straightforward and requires no chain state manipulation. No signature-forging or elevated privilege is required.

### Recommendation
Validate and process each `NakamotoBlocksData` batch independently: on a batch-level validation failure, `continue` to the next batch (from the same neighbor) instead of `break`ing out of the loop, so that subsequent valid batches from the same peer are still processed. Additionally, treat a `validate_nakamoto_blocks_push` failure the same way as `InvalidStacksBlock` — push the neighbor into `bad_neighbors` — so that peers who send malformed batches are penalized rather than being able to repeat the behavior for free every polling cycle.

### Proof of Concept
1. Attacker establishes a normal p2p connection to a victim Stacks node.
2. Attacker sends `StacksMessageType::NakamotoBlocks(NakamotoBlocksData { blocks: [bad_block] })` where `bad_block.header.consensus_hash` does not correspond to a real/valid sortition — this fails `validate_nakamoto_blocks_push`.
3. In the same polling cycle, attacker also sends `StacksMessageType::NakamotoBlocks(NakamotoBlocksData { blocks: [valid_block_1, valid_block_2, ...] })` containing entirely legitimate blocks (e.g., ones the attacker itself received/relayed from another honest peer).
4. On the victim, `network_result.pushed_nakamoto_blocks` for the attacker's `NeighborKey` contains both batches. In `process_pushed_nakamoto_blocks`, the first batch fails validation and the loop `break`s [4](#0-3) ; the second, fully valid batch is never processed or relayed by the victim in this cycle, and the attacker is not banned.
5. Repeat every cycle to persistently suppress propagation of legitimate blocks arriving through this connection.

### Citations

**File:** stackslib/src/net/relay.rs (L633-646)
```rust
    /// Given Nakamoto blocks pushed to us, verify that they correspond to expected block data.
    pub fn validate_nakamoto_blocks_push(
        burnchain: &Burnchain,
        sortdb: &SortitionDB,
        chainstate: &mut StacksChainState,
        stacks_tip: &StacksBlockId,
        nakamoto_blocks_data: &NakamotoBlocksData,
    ) -> Result<(), net_error> {
        let conn = sortdb.index_conn();
        let mut loaded_reward_sets = HashMap::new();
        let tip_sn = SortitionDB::get_canonical_burn_chain_tip(sortdb.conn())?;

        for nakamoto_block in nakamoto_blocks_data.blocks.iter() {
            // is this the right Stacks block for this sortition?
```

**File:** stackslib/src/net/relay.rs (L1660-1679)
```rust
        // process Nakamoto blocks pushed to us.
        // If a neighbor sends us an invalid Nakamoto block, then ban them.
        for (neighbor_key, relayers_and_block_data) in
            network_result.pushed_nakamoto_blocks.iter_mut()
        {
            for (relayers, nakamoto_blocks_data) in relayers_and_block_data.iter_mut() {
                let mut accepted_blocks = vec![];
                if let Err(e) = Relayer::validate_nakamoto_blocks_push(
                    burnchain,
                    sortdb,
                    chainstate,
                    &network_result.stacks_tip,
                    nakamoto_blocks_data,
                ) {
                    info!(
                        "Failed to validate Nakamoto blocks pushed from {:?}: {:?}",
                        neighbor_key, &e
                    );
                    break;
                }
```

**File:** stackslib/src/net/relay.rs (L1730-1734)
```rust
                        Err(chainstate_error::InvalidStacksBlock(msg)) => {
                            warn!("Invalid pushed Nakamoto block {}: {}", &block_id, msg);
                            bad_neighbors.push((*neighbor_key).clone());
                            break;
                        }
```
