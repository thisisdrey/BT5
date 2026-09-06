### No vulnerability found for this question.

**Reasoning:** The premise — that a peer can freely supply an unrelated `tenure_end_block_id` into `make_highest_complete_tenure_downloader()` — is refuted by the validation performed in `try_accept_tenure_info()`. Before `tenure_tip` (and thus `parent_tenure_start_block_id`, which seeds `tenure_end_block_id`) is ever accepted, the node:

1. Resolves both `remote_tenure_tip.consensus_hash` and `remote_tenure_tip.parent_consensus_hash` against its own `SortitionDB`, rejecting unknown consensus hashes. [1](#0-0) 
2. Confirms both snapshots are on the node's own canonical fork by comparing against `get_block_snapshot_by_height` on the local sortition tip's index handle, rejecting non-canonical forks. [2](#0-1) 
3. Critically, checks that `remote_tenure_tip.parent_tenure_start_block_id` equals `local_tenure_sn.winning_stacks_block_hash` — i.e., the winning Stacks block hash that the node's *own* sortition/burnchain state recorded for that tenure's sortition — rejecting the peer as stale otherwise. [3](#0-2) 

Only after this check passes does `self.tenure_tip = Some(remote_tenure_tip)` get set [4](#0-3) , and `make_highest_complete_tenure_downloader()` later builds the `NakamotoTenureDownloader` using `tenure_tip.parent_tenure_start_block_id` as the tenure-end block [5](#0-4) .

So the "highest complete tenure" end-block value is not attacker-supplied data trusted blindly — it is cross-checked against the node's own sortition DB's `winning_stacks_block_hash` for a canonical-fork sortition snapshot. A peer sending a manipulated/unrelated `tenure_end_block_id` (`parent_tenure_start_block_id`) that doesn't match the node's local sortition record is rejected with `NetError::StaleView` at that point, before any downloader is constructed. This guard directly prevents the attack scenario in the question, so the claimed equality break does not occur.

### Citations

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L176-197)
```rust
        let local_tenure_sn = SortitionDB::get_block_snapshot_consensus(
            sortdb.conn(),
            &remote_tenure_tip.consensus_hash,
        )?
        .ok_or_else(|| {
            debug!(
                "No snapshot for tenure {}",
                &remote_tenure_tip.consensus_hash
            );
            NetError::DBError(DBError::NotFoundError)
        })?;
        let parent_local_tenure_sn = SortitionDB::get_block_snapshot_consensus(
            sortdb.conn(),
            &remote_tenure_tip.parent_consensus_hash,
        )?
        .ok_or_else(|| {
            debug!(
                "No snapshot for parent tenure {}",
                &remote_tenure_tip.parent_consensus_hash
            );
            NetError::DBError(DBError::NotFoundError)
        })?;
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L199-237)
```rust
        let ih = sortdb.index_handle(&local_sort_tip.sortition_id);
        let ancestor_local_tenure_sn = ih
            .get_block_snapshot_by_height(local_tenure_sn.block_height)?
            .ok_or_else(|| {
                debug!(
                    "No tenure snapshot at burn block height {} off of sortition {} ({})",
                    local_tenure_sn.block_height,
                    &local_tenure_sn.sortition_id,
                    &local_tenure_sn.consensus_hash
                );
                NetError::DBError(DBError::NotFoundError)
            })?;

        if ancestor_local_tenure_sn.sortition_id != local_tenure_sn.sortition_id {
            // .consensus_hash is not on the canonical fork
            warn!("Unconfirmed tenure consensus hash is not canonical";
                  "peer" => %self.naddr,
                  "consensus_hash" => %remote_tenure_tip.consensus_hash);
            return Err(DBError::NotFoundError.into());
        }
        let ancestor_parent_local_tenure_sn = ih
            .get_block_snapshot_by_height(parent_local_tenure_sn.block_height)?
            .ok_or_else(|| {
                debug!(
                    "No parent tenure snapshot at burn block height {} off of sortition {} ({})",
                    local_tenure_sn.block_height,
                    &local_tenure_sn.sortition_id,
                    &local_tenure_sn.consensus_hash
                );
                NetError::DBError(DBError::NotFoundError)
            })?;

        if ancestor_parent_local_tenure_sn.sortition_id != parent_local_tenure_sn.sortition_id {
            // .parent_consensus_hash is not on the canonical fork
            warn!("Parent unconfirmed tenure consensus hash is not canonical";
                  "peer" => %self.naddr,
                  "consensus_hash" => %remote_tenure_tip.parent_consensus_hash);
            return Err(DBError::NotFoundError.into());
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L248-257)
```rust
        // parent tenure start block ID must be the winning block hash for the ongoing tenure's
        // snapshot
        if local_tenure_sn.winning_stacks_block_hash.0
            != remote_tenure_tip.parent_tenure_start_block_id.0
        {
            debug!("Ongoing tenure does not commit to highest complete tenure's start block. Treating remote peer {} as stale.", &self.naddr;
                  "remote_tenure_tip.tenure_start_block_id" => %remote_tenure_tip.parent_tenure_start_block_id,
                  "local_tenure_sn.winning_stacks_block_hash" => %local_tenure_sn.winning_stacks_block_hash);
            return Err(NetError::StaleView);
        }
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L378-384)
```rust
        self.confirmed_signer_keys = Some(confirmed_reward_set.clone());
        self.unconfirmed_signer_keys = Some(unconfirmed_reward_set.clone());
        self.confirmed_epoch_id = confirmed_epoch_id;
        self.unconfirmed_epoch_id = unconfirmed_epoch_id;
        self.tenure_tip = Some(remote_tenure_tip);

        Ok(())
```

**File:** stackslib/src/net/download/nakamoto/tenure_downloader_unconfirmed.rs (L743-754)
```rust
        let ntd = NakamotoTenureDownloader::new(
            tenure_tip.parent_consensus_hash.clone(),
            tenure_tip.consensus_hash.clone(),
            tenure_tip.parent_tenure_start_block_id.clone(),
            tenure_tip.consensus_hash.clone(),
            tenure_tip.tenure_start_block_id.clone(),
            self.naddr.clone(),
            confirmed_signer_keys.clone(),
            unconfirmed_signer_keys.clone(),
            epoch_id,
            true,
        );
```
