### Title
Inconsistent block-size bound between RPC POST `/v2/blocks/upload` and legacy P2P `BlocksDatum` allows storing/relaying a block that peers will reject - ([File: stackslib/src/chainstate/stacks/block.rs], [stackslib/src/net/api/postblock.rs], [stackslib/src/net/codec.rs])

### Summary
`StacksBlock::consensus_deserialize` does not itself enforce any total-block-size cap; it only bounds the *transaction vector* portion of the read to `MAX_MESSAGE_LEN` [1](#0-0) . The RPC POST handler for `/v2/blocks/upload` relies solely on the HTTP `Content-Length` check against `MAX_PAYLOAD_LEN` (1 + 16MiB) before calling that deserializer [2](#0-1) , whereas the legacy P2P block-push message (`BlocksDatum`, carried inside `BlocksData`) wraps each individual block in a `BoundReader` capped at the much smaller `MAX_BLOCK_LEN` constant [3](#0-2) . This produces two different effective maximum accepted block sizes depending on ingestion path.

### Finding Description
`StacksBlock`'s codec comment explicitly says "don't worry about size clamps here; do that when receiving the data from the peer network" [4](#0-3) , meaning the size clamp is expected to be applied by the *caller*, not by the type itself. The two callers apply different clamps:

- P2P legacy block push: `BlocksDatum::consensus_deserialize` bounds the single block to `MAX_BLOCK_LEN` via `BoundReader::from_reader(fd, MAX_BLOCK_LEN as u64)` [5](#0-4) .
- RPC POST `/v2/blocks/upload`: `try_parse_request` only rejects bodies whose `Content-Length` exceeds `MAX_PAYLOAD_LEN` (`1 + 16*1024*1024`, imported from `stacks_common::codec`) [2](#0-1) , then calls `StacksBlock::consensus_deserialize` directly on the raw body with no additional per-block cap [6](#0-5) .

`MAX_BLOCK_LEN` and `MAX_PAYLOAD_LEN` are separate constants (`MAX_BLOCK_LEN` imported from `crate::chainstate::stacks` in `codec.rs`, `MAX_PAYLOAD_LEN` from `stacks_common::codec`) [7](#0-6) [8](#0-7) . If `MAX_BLOCK_LEN` < `MAX_PAYLOAD_LEN` (consistent with the codec.rs comment marking the outer `BoundReader::from_reader(fd, MAX_MESSAGE_LEN as u64)` in `BlocksData`/`NakamotoBlocksData` as "loose upper-bound" and only the per-item `BlocksDatum` reader as the real per-block cap), an attacker can craft a legacy `StacksBlock` whose serialized size is strictly between `MAX_BLOCK_LEN` and `MAX_PAYLOAD_LEN`:

1. POST it to `/v2/blocks/upload/<consensus_hash>` on a victim node — it passes the `Content-Length <= MAX_PAYLOAD_LEN` check, is deserialized successfully (no per-block cap in `StacksBlock::consensus_deserialize`), and if accepted by `Relayer::process_new_anchored_block` is queued for P2P relay via `node.set_relay_message(StacksMessageType::Blocks(...))` [9](#0-8) .
2. When the victim relays this `BlocksData` message to its P2P peers, each peer's codec deserializes the inner `BlocksDatum` with a `BoundReader` capped at `MAX_BLOCK_LEN` [3](#0-2) ; since the block exceeds `MAX_BLOCK_LEN`, the bound reader will exhaust before the block finishes parsing, causing a deserialize failure and rejection of the relayed message by every honest peer.

This exactly matches the "storing then relaying a block that other peers reject outright" scenario.

### Impact Explanation
A node that accepts such an oversized block via RPC stores/processes it locally (subject to whatever downstream chainstate validation exists) but cannot successfully propagate it — its outbound relay message is malformed relative to what peers' P2P codec accepts, so the block never enters the P2P network's accepted view. This is a boundary-behavior inconsistency (CONSISTENCY equality violated: max block size accepted for storage via RPC != max block size accepted for storage via P2P) rather than an out-of-bound memory read or unauthenticated write of new state into other nodes; it does not by itself corrupt other peers' state, since their codec correctly rejects the oversized relay. The primary impact is availability/consistency: a node can be induced into an inconsistent state where a block it treats as valid/stored can never be relayed and accepted network-wide, which could contribute to chain-fork/relay confusion for that block, matching a Critical-tier bound-check-omission concern.

### Likelihood Explanation
Any unprivileged remote client that can reach the victim's RPC port can send a single crafted POST request with a legacy `StacksBlock` sized between `MAX_BLOCK_LEN` and `MAX_PAYLOAD_LEN` — no privileged secret, signature, or session state is required beyond a `consensus_hash` known to the node. The attack is a single HTTP request and is trivially repeatable.

### Recommendation
Enforce the same per-block size bound (`MAX_BLOCK_LEN`) in `RPCPostBlockRequestHandler::try_parse_request`/`parse_postblock_octets` in `stackslib/src/net/api/postblock.rs` that the P2P `BlocksDatum` codec applies, rather than relying only on the generic `MAX_PAYLOAD_LEN` HTTP body-size check. More robustly, move the size clamp into `StacksBlock::consensus_deserialize` itself (in `stackslib/src/chainstate/stacks/block.rs`) using a single canonical constant (`MAX_BLOCK_LEN`) so every caller — P2P and RPC alike — inherits the identical bound instead of re-implementing (and potentially mismatching) it per call site.

### Proof of Concept
Rust test plan (net/chainstate integration test):
1. Construct a `StacksBlock` whose serialized size is `MAX_BLOCK_LEN + 1` bytes (padding via extra valid transactions) but `<= MAX_PAYLOAD_LEN`.
2. Feed the serialized bytes through `RPCPostBlockRequestHandler::try_parse_request` (or the full HTTP handler) — assert it succeeds and returns `Ok`, and that the block is subsequently placed into `node.set_relay_message`.
3. Feed the same serialized bytes through `BlocksDatum::consensus_deserialize` (as would happen on a peer receiving a relayed `BlocksData` P2P message) — assert it returns `Err(codec_error::...)` due to the `MAX_BLOCK_LEN`-bounded `BoundReader` exhausting.
4. The divergent Ok/Err result for identical bytes across the two entry points demonstrates the broken CONSISTENCY equality.

### Citations

**File:** stackslib/src/chainstate/stacks/block.rs (L307-314)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<StacksBlock, codec_error> {
        // NOTE: don't worry about size clamps here; do that when receiving the data from the peer
        // network.  This code assumes that the block will be small enough.
        let header: StacksBlockHeader = read_next(fd)?;
        let txs: Vec<StacksTransaction> = {
            let mut bound_read = BoundReader::from_reader(fd, MAX_MESSAGE_LEN as u64);
            read_next_at_most(&mut bound_read, u32::MAX)
        }?;
```

**File:** stackslib/src/net/api/postblock.rs (L54-63)
```rust
    fn parse_postblock_octets(mut body: &[u8]) -> Result<StacksBlock, Error> {
        let block = StacksBlock::consensus_deserialize(&mut body).map_err(|e| {
            if let CodecError::DeserializeError(msg) = e {
                Error::DecodeError(format!("Failed to deserialize posted transaction: {}", msg))
            } else {
                e.into()
            }
        })?;
        Ok(block)
    }
```

**File:** stackslib/src/net/api/postblock.rs (L89-99)
```rust
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected non-zero-length body for PostBlock".to_string(),
            ));
        }

        if preamble.get_content_length() > MAX_PAYLOAD_LEN {
            return Err(Error::DecodeError(
                "Invalid Http request: PostBlock body is too big".to_string(),
            ));
        }
```

**File:** stackslib/src/net/api/postblock.rs (L217-222)
```rust
        // don't forget to forward this to the p2p network!
        if data_resp.accepted {
            node.set_relay_message(StacksMessageType::Blocks(BlocksData {
                blocks: vec![BlocksDatum(consensus_hash, block)],
            }));
        }
```

**File:** stackslib/src/net/codec.rs (L40-42)
```rust
use crate::chainstate::stacks::{
    StacksBlock, StacksMicroblock, StacksPublicKey, StacksTransaction, MAX_BLOCK_LEN,
};
```

**File:** stackslib/src/net/codec.rs (L488-497)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<BlocksDatum, codec_error> {
        let ch: ConsensusHash = read_next(fd)?;
        let block = {
            let mut bound_read = BoundReader::from_reader(fd, MAX_BLOCK_LEN as u64);
            read_next(&mut bound_read)
        }?;

        Ok(BlocksDatum(ch, block))
    }
}
```

**File:** stacks-common/src/codec/mod.rs (L242-245)
```rust
// messages can't be bigger than 16MB plus the preamble and relayers
pub const MAX_PAYLOAD_LEN: u32 = 1 + 16 * 1024 * 1024;
pub const MAX_MESSAGE_LEN: u32 =
    MAX_PAYLOAD_LEN + (PREAMBLE_ENCODED_SIZE + MAX_RELAYERS_LEN * RELAY_DATA_ENCODED_SIZE);
```
