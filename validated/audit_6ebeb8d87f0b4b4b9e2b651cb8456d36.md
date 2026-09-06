### Title
Unauthenticated `GetNakamotoInv` requests trigger unbounded, unthrottled tenure-walk compute that scales with a growing/unaligned attack surface (chain height) - (File: `stackslib/src/net/inv/nakamoto.rs`)

### Summary
`GetNakamotoInv` is a p2p request that any connected (but not privileged) peer can send at will, with no per-request compute-cost throttling, unlike push-type messages (`StackerDBPushChunk`, `Blocks`, `Microblocks`, `Transaction`) which are explicitly bandwidth-limited via `validate_*_push` calls in `ConversationP2P::sign_and_reply`/`chat`. Handling it invokes `InvGenerator::make_tenure_bitvector`, which walks backward sortition-by-sortition (up to `reward_cycle_length` iterations), performing a DB lookup (`get_sortition_info`) and a tenure lookup (`get_processed_tenure`) on every iteration.

### Finding Description
The dispatch path is:
`ConversationP2P::chat` → `handle_getnakamotoinv` → `ConversationP2P::make_getnakamotoinv_response` → `PeerNetwork::nakamoto_inv_generator.make_tenure_bitvector`. [1](#0-0) [2](#0-1) 

`make_tenure_bitvector` contains an unbounded `loop` (bounded only by `reward_cycle_length`, itself unrelated to request cost accounting) that performs a sortition-DB read and a chainstate tenure lookup on each pass: [3](#0-2) 

Unlike the *push* message types (`StackerDBPushChunk`, `Blocks`, `Microblocks`, `Transaction`, `NakamotoBlocks`), which are all routed through explicit bandwidth-accounting/throttling helpers before being processed: [4](#0-3) 

there is no equivalent throttle for `GetPoxInv`, `GetBlocksInv`, or `GetNakamotoInv` — these *pull* requests are handled and answered unconditionally regardless of how frequently the same peer sends them: [5](#0-4) 

The relevant compute-cost driver — the reward cycle / tenure count a full-history request can touch — only grows as the burnchain advances (i.e., as the number of tenures/sortitions accumulates over time), exactly mirroring the "`settleFunding` iterates over a growing set of markets" bug class: any unauthenticated peer can canvass the full history of reward cycles (`GetNakamotoInv` accepts a `consensus_hash` for *any* reward-cycle boundary, not just the most recent) and force repeated `O(reward_cycle_length)` DB/chainstate walks, with the cumulative cost across the full history growing monotonically as the chain (and thus the count of past reward cycles) grows — without any request-rate or cost throttle gating it, unlike every push-message type in the same dispatch table.

### Impact Explanation
This matches the "bounded compute DoS on a read endpoint" impact category: a single unauthenticated remote peer can repeatedly request `GetNakamotoInv` for many/all historical reward cycles, each of which forces the responding node to perform `O(reward_cycle_length)` sortition-DB and tenure lookups with no throttling gate (in contrast to how push-type messages are bandwidth-throttled at `validate_*_push`). As the chain accumulates more reward cycles over time, the total space of reward cycles an attacker can canvass — and thus the aggregate compute a single connected peer can force onto a victim node — grows without bound, unlike a fixed one-off cost.

### Likelihood Explanation
Likelihood is moderate: the attacker needs only an established, unauthenticated p2p connection (handshakes are cheap and widely permitted) to send arbitrarily many `GetNakamotoInv` messages, each of which is answered by walking a chunk of sortition/tenure history. No signature, stake, or privileged role is required to trigger this path; only network connectivity to the target node.

### Recommendation
Add rate/cost accounting for pull-type `Get*Inv` requests (`GetPoxInv`, `GetBlocksInv`, `GetNakamotoInv`) analogous to the existing `validate_stackerdb_push`/`validate_blocks_push`/`validate_microblocks_push`/`validate_transaction_push` bandwidth throttles, so that a peer that requests too many inventory bitvectors per unit time is NACKed/throttled instead of always serviced. Consider also caching/bounding the number of distinct historical reward-cycle bitvectors computed per peer per time window in `InvGenerator`.

### Proof of Concept
1. Establish a normal (unauthenticated beyond handshake) p2p connection to a target node.
2. Repeatedly send `GetNakamotoInv` messages, each with a `consensus_hash` corresponding to a different historical reward-cycle boundary (obtainable by walking `BlocksInv`/PoX responses, which are also unauthenticated), covering the full range of reward cycles since Nakamoto activation.
3. Observe that each request is answered unconditionally via `handle_getnakamotoinv` → `make_tenure_bitvector`, which performs up to `reward_cycle_length` sortition-DB + tenure lookups per request, with no throttle rejecting excessive requests — unlike the explicit throttles applied to push-type messages in the same `chat` dispatch (`stackslib/src/net/chat.rs:2288-2381`).

### Citations

**File:** stackslib/src/net/chat.rs (L1702-1749)
```rust
    /// Handle an inbound GetNakamotoInv request.
    /// Returns a reply handle to the generated message (possibly a nack)
    /// Only returns up to $reward_cycle_length bits
    pub fn make_getnakamotoinv_response(
        network: &mut PeerNetwork,
        sortdb: &SortitionDB,
        chainstate: &StacksChainState,
        get_nakamoto_inv: &GetNakamotoInvData,
    ) -> Result<StacksMessageType, net_error> {
        let _local_peer = network.get_local_peer();

        let base_snapshot_or_nack = Self::validate_consensus_hash_reward_cycle_start(
            _local_peer,
            sortdb,
            &get_nakamoto_inv.consensus_hash,
        )?;
        let base_snapshot = match base_snapshot_or_nack {
            Ok(sn) => sn,
            Err(msg) => {
                return Ok(msg);
            }
        };

        let tip = SortitionDB::get_canonical_burn_chain_tip(sortdb.conn())?;
        let reward_cycle = sortdb
            .pox_constants
            .block_height_to_reward_cycle(sortdb.first_block_height, base_snapshot.block_height)
            .ok_or(net_error::InvalidMessage)?;

        let bitvec_bools = network.nakamoto_inv_generator.make_tenure_bitvector(
            &tip,
            sortdb,
            chainstate,
            &network.stacks_tip.consensus_hash,
            &network.stacks_tip.block_hash,
            reward_cycle,
        )?;
        let nakamoto_inv = NakamotoInvData::try_from(&bitvec_bools).inspect_err(|e| {
            warn!("Failed to create a NakamotoInv response to {get_nakamoto_inv:?}: {e:?}")
        })?;

        debug!(
            "Reply NakamotoInv for {} (rc {}): {:?}",
            &get_nakamoto_inv.consensus_hash, reward_cycle, &nakamoto_inv
        );

        Ok(StacksMessageType::NakamotoInv(nakamoto_inv))
    }
```

**File:** stackslib/src/net/chat.rs (L2288-2316)
```rust
        let res = match msg.payload {
            StacksMessageType::GetNeighbors => self.handle_getneighbors(network, &msg.preamble),
            StacksMessageType::GetPoxInv(ref getpoxinv) => {
                self.handle_getpoxinv(network, sortdb, &msg.preamble, getpoxinv)
            }
            StacksMessageType::GetBlocksInv(ref get_blocks_inv) => {
                self.handle_getblocksinv(network, sortdb, chainstate, &msg.preamble, get_blocks_inv)
            }
            StacksMessageType::GetNakamotoInv(ref get_nakamoto_inv) => self.handle_getnakamotoinv(
                network,
                sortdb,
                chainstate,
                &msg.preamble,
                get_nakamoto_inv,
            ),
            StacksMessageType::Blocks(_) => {
                monitoring::increment_stx_blocks_received_counter();

                // not handled here, but do some accounting -- we can't receive blocks too often,
                // so close this conversation if we do.
                match self.validate_blocks_push(network, &msg.preamble, msg.relayers.clone())? {
                    Some(handle) => Ok(handle),
                    None => {
                        // will forward upstream
                        return Ok(Some(msg));
                    }
                }
            }
            StacksMessageType::Microblocks(_) => {
```

**File:** stackslib/src/net/chat.rs (L2350-2366)
```rust
            StacksMessageType::StackerDBGetChunkInv(ref getchunkinv) => {
                self.handle_stacker_db_getchunkinv(network, chainstate, &msg.preamble, getchunkinv)
            }
            StacksMessageType::StackerDBGetChunk(ref getchunk) => {
                self.handle_stacker_db_getchunk(network, &msg.preamble, getchunk)
            }
            StacksMessageType::StackerDBChunk(_) | StacksMessageType::StackerDBPushChunk(_) => {
                // not handled here, but do some accounting -- we can't receive too many
                // stackerdb chunks per second
                match self.validate_stackerdb_push(network, &msg.preamble, msg.relayers.clone())? {
                    Some(handle) => Ok(handle),
                    None => {
                        // will forward upstream
                        return Ok(Some(msg));
                    }
                }
            }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L396-448)
```rust
        loop {
            let cur_reward_cycle = sortdb
                .pox_constants
                .block_height_to_reward_cycle(sortdb.first_block_height, cur_height)
                .ok_or(NetError::ChainstateError(
                    "block height comes before system start".into(),
                ))?;
            if cur_reward_cycle < reward_cycle {
                // done scanning this reward cycle
                break;
            }
            let cur_sortition_info = self.get_sortition_info(sortdb, &cur_consensus_hash)?;
            let parent_sortition_consensus_hash = cur_sortition_info.parent_consensus_hash.clone();

            trace!("Get sortition and tenure info for height {cur_height}. cur_consensus_hash = {cur_consensus_hash}, cur_tenure_info = {cur_tenure_opt:?}, parent_sortition_consensus_hash = {parent_sortition_consensus_hash}");

            if let Some(cur_tenure_info) = cur_tenure_opt.as_ref() {
                // a tenure was active when this sortition happened...
                if cur_tenure_info.tenure_id_consensus_hash == cur_consensus_hash {
                    // ...and this tenure started in this sortition
                    trace!("Tenure was started for {cur_consensus_hash} (height {cur_height})");
                    tenure_status.push(true);
                    cur_tenure_opt = self.get_processed_tenure(
                        chainstate,
                        nakamoto_tip_ch,
                        nakamoto_tip_bh,
                        &cur_tenure_info.parent_tenure_id_consensus_hash,
                    )?;
                } else {
                    // ...but this tenure did not start in this sortition
                    trace!("Tenure was NOT started for {cur_consensus_hash} (bit {cur_height})");
                    tenure_status.push(false);
                }
            } else {
                // no active tenure during this sortition. Check the parent sortition to see if a
                // tenure begain there.
                trace!("No winning sortition for {cur_consensus_hash} (bit {cur_height})");
                tenure_status.push(false);
                cur_tenure_opt = self.get_processed_tenure(
                    chainstate,
                    nakamoto_tip_ch,
                    nakamoto_tip_bh,
                    &parent_sortition_consensus_hash,
                )?;
            }

            // next sortition
            cur_consensus_hash = parent_sortition_consensus_hash;
            if cur_height == 0 {
                break;
            }
            cur_height = cur_height.saturating_sub(1);
        }
```
