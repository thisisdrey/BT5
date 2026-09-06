[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** stackslib/src/net/inv/nakamoto.rs (L459-481)
```rust
#[derive(Debug, PartialEq, Clone)]
pub struct NakamotoTenureInv {
    /// Bitmap of which tenures a peer has.
    /// Maps reward cycle to bitmap.
    pub tenures_inv: BTreeMap<u64, BitVec<2100>>,
    /// Time of last update, in seconds
    pub last_updated_at: u64,
    /// Burn block height of first sortition
    pub first_block_height: u64,
    /// Length of reward cycle
    pub reward_cycle_len: u64,
    /// Which neighbor is this for
    pub neighbor_address: NeighborAddress,

    /// The fields below are used for synchronizing this particular peer's inventories.
    /// Currently tracked reward cycle
    pub cur_reward_cycle: u64,
    /// Status of this node.
    /// True if we should keep talking to it; false if not
    pub online: bool,
    /// Last time we began talking to this peer
    pub start_sync_time: u64,
}
```

**File:** stackslib/src/net/inv/nakamoto.rs (L591-594)
```rust
    /// Get the reward cycle we're sync'ing for
    pub fn reward_cycle(&self) -> u64 {
        self.cur_reward_cycle
    }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L655-672)
```rust
    pub fn getnakamotoinv_try_finish(
        &mut self,
        network: &mut PeerNetwork,
        reply: StacksMessage,
    ) -> Result<bool, NetError> {
        match reply.payload {
            StacksMessageType::NakamotoInv(inv_data) => {
                debug!(
                    "{:?}: got NakamotoInv from {:?}: {:?}",
                    network.get_local_peer(),
                    &self.neighbor_address,
                    &inv_data
                );

                let ret = self.merge_tenure_inv(inv_data.tenures, self.reward_cycle());
                self.next_reward_cycle();
                return Ok(ret);
            }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L760-773)
```rust
    fn load_consensus_hash_for_reward_cycle(
        sortdb: &SortitionDB,
        reward_cycle: u64,
    ) -> Result<Option<ConsensusHash>, NetError> {
        let reward_cycle_start_height = sortdb
            .pox_constants
            .reward_cycle_to_block_height(sortdb.first_block_height, reward_cycle);
        let sn = SortitionDB::get_canonical_burn_chain_tip(sortdb.conn())?;
        let ih = sortdb.index_handle(&sn.sortition_id);
        let ch_opt = ih
            .get_block_snapshot_by_height(reward_cycle_start_height)?
            .map(|sn| sn.consensus_hash);
        Ok(ch_opt)
    }
```

**File:** stackslib/src/net/inv/nakamoto.rs (L825-833)
```rust
    /// Make a getnakamotoinv message
    fn make_getnakamotoinv(&self, reward_cycle: u64) -> Option<StacksMessageType> {
        let Some(ch) = self.reward_cycle_consensus_hashes.get(&reward_cycle) else {
            return None;
        };
        Some(StacksMessageType::GetNakamotoInv(GetNakamotoInvData {
            consensus_hash: ch.clone(),
        }))
    }
```
