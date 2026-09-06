[1](#0-0)

### Citations

**File:** stackslib/src/clarity_vm/database/ephemeral.rs (L393-415)
```rust
        // this bhh is not ephemeral, so it might be disk-backed.
        self.read_only_marf
            .check_ancestor_block_hash(&bhh)
            .map_err(|e| match e {
                Error::NotFoundError => {
                    test_debug!("No such block {:?} (NotFoundError)", &bhh);
                    RuntimeError::UnknownBlockHeaderHash(BlockHeaderHash(bhh.0))
                }
                Error::NonMatchingForks(_bh1, _bh2) => {
                    test_debug!(
                        "No such block {:?} (NonMatchingForks({}, {}))",
                        &bhh,
                        BlockHeaderHash(_bh1),
                        BlockHeaderHash(_bh2)
                    );
                    RuntimeError::UnknownBlockHeaderHash(BlockHeaderHash(bhh.0))
                }
                _ => panic!("ERROR: Unexpected MARF failure: {}", e),
            })?;

        let old_tip = mem::replace(&mut self.open_tip, EphemeralTip::Disk(bhh));
        Ok(old_tip.into_block_id())
    }
```
