[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/chainstate/nakamoto/tenure.rs (L697-751)
```rust
        if tenure_payload.prev_tenure_consensus_hash != FIRST_BURNCHAIN_CONSENSUS_HASH {
            // the parent sortition must exist, must be canonical, and must be an ancestor of the
            // sortition for the given consensus hash.
            let Some(prev_sn) = Self::check_valid_consensus_hash(
                sort_handle,
                &tenure_payload.prev_tenure_consensus_hash,
            )?
            else {
                return Ok(None);
            };
            match tenure_payload.cause {
                TenureChangeCause::BlockFound => {
                    if prev_sn.block_height >= tenure_sn.block_height {
                        // parent comes after child
                        warn!("Invalid tenure-change: parent snapshot comes at or after current tenure"; "tenure_consensus_hash" => %tenure_payload.tenure_consensus_hash, "prev_tenure_consensus_hash" => %tenure_payload.prev_tenure_consensus_hash);
                        return Ok(None);
                    }
                }
                TenureChangeCause::Extended
                | TenureChangeCause::ExtendedRuntime
                | TenureChangeCause::ExtendedReadCount
                | TenureChangeCause::ExtendedReadLength
                | TenureChangeCause::ExtendedWriteCount
                | TenureChangeCause::ExtendedWriteLength => {
                    // prev and current tenure consensus hashes must be identical
                    if prev_sn.consensus_hash != tenure_sn.consensus_hash {
                        warn!("Invalid tenure-change extension: parent snapshot is not the same as the current tenure snapshot"; "tenure_consensus_hash" => %tenure_payload.tenure_consensus_hash, "prev_tenure_consensus_hash" => %tenure_payload.prev_tenure_consensus_hash);
                        return Ok(None);
                    }
                }
            }

            if prev_sn.block_height > sortition_sn.block_height {
                // parent comes after tip
                warn!("Invalid tenure-change: parent snapshot comes after current tip"; "burn_view_consensus_hash" => %tenure_payload.burn_view_consensus_hash, "prev_tenure_consensus_hash" => %tenure_payload.prev_tenure_consensus_hash);
                return Ok(None);
            }

            // is the parent a shadow block?
            // Only possible if the parent is also a nakamoto block
            let is_parent_shadow_block = NakamotoChainState::get_nakamoto_block_version(
                headers_conn.sqlite(),
                &block_header.parent_block_id,
            )?
            .map(NakamotoBlockHeader::is_shadow_block_version)
            .unwrap_or(false);

            if !is_parent_shadow_block && !prev_sn.sortition {
                // parent wasn't a shadow block (we expect a sortition), but this wasn't a sortition-induced tenure change
                warn!("Invalid tenure-change: no block found";
                      "prev_tenure_consensus_hash" => %tenure_payload.prev_tenure_consensus_hash
                );
                return Ok(None);
            }
        }
```

**File:** stackslib/src/chainstate/nakamoto/tenure.rs (L761-771)
```rust
        // What tenure are we building off of?  This is the tenure in which the parent block
        // resides.  Note that if this block is a tenure-extend block, then parent_block_id and
        // this block reside in the same tenure (but this block will insert a tenure-extend record
        // into the tenure-changes table).
        let Some(parent_tenure) =
            Self::get_ongoing_tenure(headers_conn, &block_header.parent_block_id)?
        else {
            // not building off of a previous Nakamoto tenure.  This is the first tenure change.  It should point to an epoch
            // 2.x block.
            return Self::check_first_nakamoto_tenure_change(headers_conn.sqlite(), tenure_payload);
        };
```

**File:** stackslib/src/chainstate/nakamoto/tenure.rs (L865-871)
```rust
        if Self::check_nakamoto_tenure(headers_tx, handle, &block.header, tenure_payload)?.is_none()
        {
            return Err(ChainstateError::InvalidStacksTransaction(
                "Invalid tenure tx".into(),
                false,
            ));
        };
```
