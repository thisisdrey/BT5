[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** stackslib/src/net/relay.rs (L821-832)
```rust
        // find the snapshot of the parent of this block
        let parent_block_snapshot = match sort_ic
            .find_parent_snapshot_for_stacks_block(consensus_hash, &block.block_hash())?
        {
            Some(sn) => sn,
            None => {
                // doesn't correspond to a PoX-valid sortition
                return Ok(BlockAcceptResponse::Rejected(
                    "Block does not correspond to a known sortition".into(),
                ));
            }
        };
```

**File:** stackslib/src/net/relay.rs (L1204-1226)
```rust
            match Relayer::process_new_anchored_block(
                sort_ic,
                chainstate,
                consensus_hash,
                block,
                *download_time,
            ) {
                Ok(accept_response) => {
                    if BlockAcceptResponse::Accepted == accept_response {
                        debug!(
                            "Accepted downloaded block {}/{}",
                            consensus_hash,
                            &block.block_hash()
                        );
                        new_blocks.insert((*consensus_hash).clone(), block.clone());
                    } else {
                        debug!(
                            "Rejected downloaded block {}/{}: {:?}",
                            consensus_hash,
                            &block.block_hash(),
                            &accept_response
                        );
                    }
```

**File:** stackslib/src/net/relay.rs (L1300-1307)
```rust
                match Relayer::validate_blocks_push(sort_ic, blocks_data) {
                    Ok(_) => {}
                    Err(_) => {
                        // punish this peer
                        bad_neighbors.push((*neighbor_key).clone());
                        break;
                    }
                }
```

**File:** stackslib/src/chainstate/burn/db/sortdb.rs (L3851-3886)
```rust
    pub fn find_parent_snapshot_for_stacks_block(
        &self,
        consensus_hash: &ConsensusHash,
        block_hash: &BlockHeaderHash,
    ) -> Result<Option<BlockSnapshot>, db_error> {
        let db_handle = SortitionHandleConn::open_reader_consensus(self, consensus_hash)?;
        let parent_block_snapshot =
            match db_handle.get_block_snapshot_of_parent_stacks_block(consensus_hash, block_hash) {
                Ok(Some((_, sn))) => {
                    debug!(
                        "Parent of {}/{} is {}/{}",
                        consensus_hash, block_hash, sn.consensus_hash, sn.winning_stacks_block_hash
                    );
                    sn
                }
                Ok(None) => {
                    debug!(
                        "Received block with unknown parent snapshot: {}/{}",
                        consensus_hash, block_hash,
                    );
                    return Ok(None);
                }
                Err(db_error::InvalidPoxSortition) => {
                    warn!(
                        "Received block {}/{} on a non-canonical PoX sortition",
                        consensus_hash, block_hash,
                    );
                    return Ok(None);
                }
                Err(e) => {
                    return Err(e);
                }
            };

        Ok(Some(parent_block_snapshot))
    }
```

**File:** stackslib/src/chainstate/stacks/db/blocks.rs (L3117-3141)
```rust
        // sortition-winning block commit for this block?
        let block_hash = block.block_hash();
        let (block_commit, parent_stacks_chain_tip) = match db_handle
            .get_block_snapshot_of_parent_stacks_block(consensus_hash, &block_hash)
        {
            Ok(Some(bc)) => bc,
            Ok(None) => {
                // unsoliciated
                warn!(
                    "Received unsolicited block: {}/{}",
                    consensus_hash, block_hash
                );
                return Ok(None);
            }
            Err(db_error::InvalidPoxSortition) => {
                warn!(
                    "Received unsolicited block on non-canonical PoX fork: {}/{}",
                    consensus_hash, block_hash
                );
                return Ok(None);
            }
            Err(e) => {
                return Err(e.into());
            }
        };
```
