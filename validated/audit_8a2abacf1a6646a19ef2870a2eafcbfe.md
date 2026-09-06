Based on the code, the premise underlying this question does not hold.

**Where consensus hashes come from:** `available_tenures` keys are anchored to `self.wanted_tenures`, which is populated by `load_wanted_tenures` directly from `SortitionDB` snapshots (`cursor.consensus_hash`, `cursor.winning_stacks_block_hash`), not from peer-supplied data. [1](#0-0) 

In `find_available_tenures`, the hash map is pre-seeded with an entry for every consensus hash in `wanted_tenures` (line 568), and peer inventories are only used to append `NeighborAddress`es to existing keys — a peer's inventory bit can never create a new, fictitious `ConsensusHash` key. [2](#0-1) 

**No fictitious slot possible:** Since every key in `available_tenures` corresponds to a real, sortition-confirmed tenure, the scenario of "a non-existent tenure consensus hash" being injected by colluding peers is not reachable — sybils can only lie about (set/clear) bits for tenures that genuinely exist in the local view of the burnchain.

**Downstream verification defeats the false-serving path:** Even if sybils falsely claim to have a real tenure, `NakamotoTenureDownloader::try_accept_tenure_start_block` requires the delivered block's ID to exactly equal the pre-computed `tenure_start_block_id` (derived from the sortition-winning block hash) and requires valid signer signatures verified against the actual signer set for that reward cycle. [3](#0-2) 

Sybils without the real signed block cannot pass this check, so any attempted download against them fails cryptographically rather than succeeding with forged/non-canonical data.

**No permanent head-of-line stall:** In `make_tenure_downloaders`, when a scheduled tenure's neighbor list is exhausted (`neighbors.is_empty()`), the item is popped from `schedule` and the next tenure is processed — it isn't blocked forever behind an unresolvable head.

<invoke name="grep_search">
<parameter name="pattern">clear_available_peers|deprioritize|attempt_failed_tenures</parameter>
<parameter name="repos">["ThankGodontt/stacks-core--008"]</parameter>
<parameter name="include">stackslib/src/net/download/nakamoto/tenure_downloader_set.rs</parameter>
</invoke>

### Citations

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L165-180)
```rust
        let mut cursor = ih
            .get_block_snapshot_by_height(last_block_height.saturating_sub(1))?
            .ok_or(DBError::NotFoundError)?;
        while cursor.block_height >= first_block_height {
            debug!(
                "Load sortition {}/{} burn height {}",
                &cursor.consensus_hash, &cursor.winning_stacks_block_hash, cursor.block_height
            );
            wanted_tenures.push(WantedTenure::new(
                cursor.consensus_hash,
                StacksBlockId(cursor.winning_stacks_block_hash.0),
                cursor.block_height,
            ));
            cursor = SortitionDB::get_block_snapshot(&ih, &cursor.parent_sortition_id)?
                .ok_or(DBError::NotFoundError)?;
        }
```

**File:** stackslib/src/net/download/nakamoto/download_state_machine.rs (L565-609)
```rust
    ) -> HashMap<ConsensusHash, Vec<NeighborAddress>> {
        let mut available: HashMap<ConsensusHash, Vec<NeighborAddress>> = HashMap::new();
        for wt in wanted_tenures.iter() {
            available.insert(wt.tenure_id_consensus_hash.clone(), vec![]);
        }

        while let Some((naddr, inv)) = inventory_iter.next() {
            let Some(rc_inv) = inv.tenures_inv.get(&reward_cycle) else {
                // this peer has no inventory data for this reward cycle
                debug!(
                    "Peer {} has no inventory for reward cycle {}",
                    naddr, reward_cycle
                );
                debug!("Peer {} has the following inventory data: {:?}", naddr, inv);
                continue;
            };
            for (i, wt) in wanted_tenures.iter().enumerate() {
                if wt.processed {
                    continue;
                }

                let (ch, ibh) = (&wt.tenure_id_consensus_hash, &wt.winning_block_id);
                if ibh == &StacksBlockId([0x00; 32]) {
                    continue;
                }

                let bit = u16::try_from(i).expect("FATAL: more sortitions than u16::MAX");
                if !rc_inv.get(bit).unwrap_or(false) {
                    // this neighbor does not have this tenure
                    debug!(
                        "Peer {} does not have sortition #{} in reward cycle {} (wt {:?})",
                        naddr, bit, reward_cycle, &wt
                    );
                    continue;
                }

                if let Some(neighbor_list) = available.get_mut(ch) {
                    neighbor_list.push(naddr.clone());
                } else {
                    available.insert(ch.clone(), vec![naddr.clone()]);
                }
            }
        }
        available
    }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader.rs (L178-210)
```rust
    pub fn try_accept_tenure_start_block(
        &mut self,
        tenure_start_block: NakamotoBlock,
    ) -> Result<(), NetError> {
        let NakamotoTenureDownloadState::GetTenureStartBlock(..) = &self.state else {
            // not the right state for this
            warn!("Invalid state for this method";
                  "state" => %self.state);
            return Err(NetError::InvalidState);
        };

        if self.tenure_start_block_id != tenure_start_block.header.block_id() {
            // not the block we were expecting
            warn!("Invalid tenure-start block: unexpected";
                  "tenure_id" => %self.tenure_id_consensus_hash,
                  "tenure_id_start_block" => %self.tenure_start_block_id,
                  "tenure_start_block ID" => %tenure_start_block.header.block_id(),
                  "state" => %self.state);
            return Err(NetError::InvalidMessage);
        }

        if let Err(e) = tenure_start_block
            .header
            .verify_signer_signatures(&self.start_signer_keys, self.epoch_id)
        {
            // signature verification failed
            warn!("Invalid tenure-start block: bad signer signature";
                   "tenure_id" => %self.tenure_id_consensus_hash,
                   "block.header.block_id" => %tenure_start_block.header.block_id(),
                   "state" => %self.state,
                   "error" => %e);
            return Err(NetError::InvalidMessage);
        }
```
