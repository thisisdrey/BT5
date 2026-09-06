[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/net/chat.rs (L2240-2242)
```rust
    /// Validate a pushed Nakamoto block list.
    /// Update bandwidth accounting, but forward the blocks along if we can accept them.
    /// Possibly return a reply handle for a NACK if we throttle the remote sender
```

**File:** stackslib/src/net/chat.rs (L2266-2276)
```rust
        if self.connection.options.max_nakamoto_block_push_bandwidth > 0
            && self.stats.get_nakamoto_block_push_bandwidth()
                > (self.connection.options.max_nakamoto_block_push_bandwidth as f64)
        {
            debug!("{:?}: Neighbor {:?} exceeded max Nakamoto block push bandwidth of {} bytes/sec (currently at {})", self, &self.to_neighbor_key(), self.connection.options.max_nakamoto_block_push_bandwidth, self.stats.get_nakamoto_block_push_bandwidth());
            return self
                .reply_nack(local_peer, chain_view, preamble, NackErrorCodes::Throttled)
                .map(Some);
        }

        Ok(None)
```

**File:** stackslib/src/net/chat.rs (L2303-2315)
```rust
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
```
