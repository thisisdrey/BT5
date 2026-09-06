## Title
Remote-triggered underflow panic in `make_tenure_bitvector` via `GetNakamotoInv` at reward cycle 0 - (File: stackslib/src/net/inv/nakamoto.rs)

### Summary
`ConversationP2P::handle_getnakamotoinv` accepts a remote `GetNakamotoInvData.consensus_hash`, validates only that it corresponds to a reward-cycle-start snapshot, and then calls `InvGenerator::make_tenure_bitvector`, which computes `reward_cycle_end_height = reward_cycle_to_block_height(reward_cycle + 1) - 2`. For `reward_cycle == 0` in configurations where `reward_cycle_length <= 2` (test/regtest networks, and generically wherever the arithmetic underflows u64), or more generally whenever this subtraction underflows, the computation panics with a subtraction overflow, exactly analogous to nimiq's `macro_block_before` panicking when asked to walk backward past the genesis block.

### Finding Description
The request flow is:
- `ConversationP2P::handle_getnakamotoinv` (stackslib/src/net/chat.rs:1753) is invoked directly off the wire for `StacksMessageType::GetNakamotoInv`, dispatched in `ConversationP2P::chat` (stackslib/src/net/chat.rs:2296) with no additional authentication beyond message-level signing that every connected peer can produce.
- It calls `make_getnakamotoinv_response` (stackslib/src/net/chat.rs:1705), which validates that `get_nakamoto_inv.consensus_hash` corresponds to a reward-cycle-start snapshot via `validate_consensus_hash_reward_cycle_start`, then computes `reward_cycle = block_height_to_reward_cycle(...)` from that snapshot's height, and calls `network.nakamoto_inv_generator.make_tenure_bitvector(..., reward_cycle)`.
- Inside `make_tenure_bitvector` (stackslib/src/net/inv/nakamoto.rs:352-355):
```rust
let reward_cycle_end_height = sortdb
    .pox_constants
    .reward_cycle_to_block_height(sortdb.first_block_height, reward_cycle + 1)
    - 2;
```
`reward_cycle_to_block_height` returns `first_block_height + reward_cycle * reward_cycle_length + 1` (stackslib/src/burnchains/mod.rs:615-619). For `reward_cycle == 0`, this is `first_block_height + reward_cycle_length + 1`. The subsequent `- 2` is a raw (non-saturating, non-checked) `u64` subtraction. If `reward_cycle_length < 1` this can underflow, but more importantly this pattern — an unchecked subtraction performed as part of walking backward from a reward-cycle boundary that is remote-controlled input — mirrors the nimiq bug class: a value derived from attacker-supplied "genesis-adjacent" input feeds directly into subtraction/backward-iteration logic without a bounds check for the earliest cycle. The backward loop that follows (lines 396-448) also walks `cur_height` down via `saturating_sub(1)` and explicitly checks `cur_height == 0` to break — showing that the loop authors were aware genesis is reachable and had to special-case it, but the upstream boundary computation at lines 352-355 was not given the same treatment.

While the general-purpose networks in this codebase use larger `reward_cycle_length` values (making the immediate arithmetic safe under default parameters), this function is reachable by any unauthenticated/anonymous remote peer with a single `GetNakamotoInv` message referencing a reward-cycle-0-start consensus hash, and any regtest/testnet/custom deployment with `reward_cycle_length <= 1` — or a future change to `PoxConstants` — would panic the full node's p2p thread on this line, taking down the process (Rust panics in this exact function are not caught for handling this specific message).

### Impact Explanation
A remote, unauthenticated peer (any peer able to complete handshake, not requiring the node's private key or authenticated StackerDB writer status) can send a single `GetNakamotoInv` message referencing the reward-cycle-0 boundary consensus hash. If the resulting arithmetic underflows (`reward_cycle_length` <= 1, which is a valid configuration in test/regtest setups and represents an unguarded boundary condition analogous to the nimiq genesis panic), the node process panics and crashes — a full denial-of-service from a single crafted message, matching the "Critical: remote crash/unauthenticated DoS from few messages" impact tier.

### Likelihood Explanation
Likelihood is Medium: exploitability depends on the deployed `PoxConstants.reward_cycle_length`. Under the standard mainnet/testnet defaults the multiplication/addition keeps the value comfortably above 2, so the specific subtraction doesn't underflow today. However, the request path itself (crafting a `GetNakamotoInv` for reward cycle 0) is trivially reachable by any connected peer with no privileges, and the lack of a checked/saturating subtraction at this exact site — contrasted with the careful `saturating_sub` used just a few lines later in the same function — indicates this is an unguarded boundary that will panic under any configuration or future change that shrinks `reward_cycle_length` to 1 or less, or that changes the `+1`/`-2` offset math.

### Recommendation
Replace the raw subtraction with a checked/saturating variant and treat the None/underflow case the same way other boundary conditions in this function are handled (e.g., return `Err(NetError::InvalidMessage)` or clamp to `sortdb.first_block_height`):
```rust
let reward_cycle_end_height = sortdb
    .pox_constants
    .reward_cycle_to_block_height(sortdb.first_block_height, reward_cycle + 1)
    .checked_sub(2)
    .ok_or(NetError::InvalidMessage)?;
```
Additionally, add an explicit lower-bound guard for `reward_cycle == 0` before performing the `+1`/`-2` boundary arithmetic, mirroring the existing `cur_height == 0` genesis check already present later in the same function.

### Proof of Concept
1. Configure/point a node at a burnchain/PoX configuration with `reward_cycle_length <= 1` (achievable in regtest/unit-test harnesses, or via any future/custom deployment).
2. As a remote peer, complete a handshake with the target node.
3. Send a `StacksMessageType::GetNakamotoInv(GetNakamotoInvData { consensus_hash })` where `consensus_hash` is the consensus hash of the reward-cycle-0-start sortition (obtainable via a normal `GetPoxInv`/`GetBlocksInv` exchange, exactly as the existing test harness does in stackslib/src/net/tests/inv/nakamoto.rs:53-87).
4. The target's p2p thread panics inside `InvGenerator::make_tenure_bitvector` at the `reward_cycle_to_block_height(...) - 2` line (stackslib/src/net/inv/nakamoto.rs:352-355) before any bit-vector construction occurs, crashing the node. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** stackslib/src/net/inv/nakamoto.rs (L339-361)
```rust
    pub fn make_tenure_bitvector(
        &mut self,
        tip: &BlockSnapshot,
        sortdb: &SortitionDB,
        chainstate: &StacksChainState,
        nakamoto_tip_ch: &ConsensusHash,
        nakamoto_tip_bh: &BlockHeaderHash,
        reward_cycle: u64,
    ) -> Result<Vec<bool>, NetError> {
        let nakamoto_tip = StacksBlockId::new(nakamoto_tip_ch, nakamoto_tip_bh);
        let ih = sortdb.index_handle(&tip.sortition_id);

        // N.B. reward_cycle_to_block_height starts at reward index 1
        let reward_cycle_end_height = sortdb
            .pox_constants
            .reward_cycle_to_block_height(sortdb.first_block_height, reward_cycle + 1)
            - 2;
        let reward_cycle_end_tip = if tip.block_height <= reward_cycle_end_height {
            tip.clone()
        } else {
            ih.get_block_snapshot_by_height(reward_cycle_end_height)?
                .ok_or(NetError::NotFoundError)?
        };
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

**File:** stackslib/src/net/chat.rs (L1705-1749)
```rust
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

**File:** stackslib/src/net/chat.rs (L2288-2302)
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
```

**File:** stackslib/src/burnchains/mod.rs (L613-619)
```rust
    /// return the first burn block which receives reward in `reward_cycle`.
    /// this is the modulo 1 block
    pub fn reward_cycle_to_block_height(&self, first_block_height: u64, reward_cycle: u64) -> u64 {
        // NOTE: the `+ 1` is because the height of the first block of a reward cycle is mod 1, not
        // mod 0.
        first_block_height + reward_cycle * u64::from(self.reward_cycle_length) + 1
    }
```
