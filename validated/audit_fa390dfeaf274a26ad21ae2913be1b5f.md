[1](#0-0)

### Citations

**File:** stackslib/src/net/codec.rs (L50-60)
```rust
impl Preamble {
    /// Make an empty preamble with the given version and fork-set identifier, and payload length.
    pub fn new(
        peer_version: u32,
        network_id: u32,
        block_height: u64,
        burn_block_hash: &BurnchainHeaderHash,
        stable_block_height: u64,
        stable_burn_block_hash: &BurnchainHeaderHash,
        payload_len: u32,
    ) -> Preamble {
```
