### Title
Remote unauthenticated crash via attacker-controlled `preamble.payload_len` violating hard-coded `assert!` invariants - (File: stackslib/src/net/chat.rs)

### Summary
The audited bug class is a broken equality between a value the protocol *assumes* is bounded/consistent and the actual attacker-controlled value that reaches it, leading to consensus/chain failure. The closest reachable analog in this repo's scope is not an arithmetic under/overflow but a structurally identical fault: `Conversation*::validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, `validate_stackerdb_push`, and `validate_nakamoto_block_push` in `stackslib/src/net/chat.rs` all begin with a hard `assert!` on `preamble.payload_len`, a field that is taken verbatim from the wire and never validated against a minimum before these functions run.

### Finding Description
Each of these validators opens with: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

and then immediately performs an unchecked subtraction on the same field: [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9) 

`preamble.payload_len` is a header field populated directly from the sender's declared value during preamble parsing; message-length enforcement earlier in the pipeline only checks an *upper* bound (`payload_len as u32 >= MAX_MESSAGE_LEN`) via `consume_preamble`, with no corresponding lower-bound check: [11](#0-10) 

Because the message-type dispatch (which routes a parsed `Blocks`/`Microblocks`/`Transaction`/`StackerDBPushChunk`/`NakamotoBlocks` push message to the corresponding `validate_*` function) does not depend on the numeric value of `payload_len` being consistent with the actual serialized body size, a peer can send a syntactically valid message (e.g. an empty `relayers` vector plus an empty or minimal body) while setting the header's `payload_len` to any value ≤ 5 (or ≤ 1 for the single-byte-prefix variants). This breaks the implicit equality the code assumes between "declared payload length" and "actual payload structure," and the `assert!` fires, unwinding/panicking the thread handling that peer's conversation.

### Impact Explanation
An `assert!` failure in Rust panics the calling thread. In the P2P networking stack this is reachable from any unauthenticated/unprivileged remote peer that has completed (or is completing) a handshake — no special role, key, or elevated privilege is required, only the ability to send one crafted `Blocks`/`Microblocks`/`Transaction`/`StackerDBPushChunk`/`NakamotoBlocks` push message with a header `payload_len` at or below the hard-coded threshold. Depending on how the P2P event loop supervises conversation threads, this can crash or destabilize the node's networking component with a single malicious message, which matches the "Critical – remote crash/unauthenticated DoS from few messages" tier.

### Likelihood Explanation
Likelihood is high for any attacker capable of crafting a raw P2P `StacksMessage`: `payload_len` is a plain header integer under full sender control, and the assert's precondition is never independently re-validated against the actually-parsed message contents before these five functions execute. No cryptographic material, rate limiting, or privileged path is required to trigger it — a single push-type message with a manipulated declared length suffices.

### Recommendation
Replace the `assert!` invariants in `validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, `validate_stackerdb_push`, and `validate_nakamoto_block_push` with graceful error handling (`Err(net_error::InvalidMessage)` or similar) instead of panicking, and use `checked_sub` (or equivalent saturating arithmetic) when computing byte counts from `preamble.payload_len`, so that an inconsistent or undersized declared length is rejected as a malformed message rather than causing a process-level panic.

### Proof of Concept
1. Establish a P2P handshake with a target node as an ordinary peer (no special privileges needed).
2. Construct a `StacksMessage` whose payload is a `BlocksData` (or `MicroblocksData`/`Transaction`/`StackerDBPushChunkData`/`NakamotoBlocksData`) push message with an empty `relayers` vector and minimal/empty body, but manually set the `Preamble.payload_len` field to a value ≤ 5 (or ≤ 1 for the single-prefix variants) when serializing the preamble — independent from what the actual body would naturally encode to.
3. Send this message to the target node's P2P listener.
4. Once the node's conversation-handling code dispatches the parsed message to the matching `validate_*_push` function, the leading `assert!(preamble.payload_len > 5)` (or `> 1`) evaluates false and panics the handling thread, crashing/disrupting the node's networking component.

### Citations

**File:** stackslib/src/net/chat.rs (L2095-2095)
```rust
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length
```

**File:** stackslib/src/net/chat.rs (L2106-2106)
```rust
        self.stats.add_block_push((preamble.payload_len as u64) - 5);
```

**File:** stackslib/src/net/chat.rs (L2135-2135)
```rust
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length
```

**File:** stackslib/src/net/chat.rs (L2150-2150)
```rust
            .add_microblocks_push((preamble.payload_len as u64) - 5);
```

**File:** stackslib/src/net/chat.rs (L2172-2172)
```rust
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```

**File:** stackslib/src/net/chat.rs (L2187-2187)
```rust
            .add_transaction_push((preamble.payload_len as u64) - 1);
```

**File:** stackslib/src/net/chat.rs (L2210-2210)
```rust
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```

**File:** stackslib/src/net/chat.rs (L2225-2225)
```rust
            .add_stackerdb_push((preamble.payload_len as u64) - 1);
```

**File:** stackslib/src/net/chat.rs (L2249-2249)
```rust
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```

**File:** stackslib/src/net/chat.rs (L2264-2264)
```rust
            .add_nakamoto_block_push((preamble.payload_len as u64) - 1);
```

**File:** stackslib/src/net/connection.rs (L716-723)
```rust
                if let Some(payload_len) = protocol.payload_len(&preamble) {
                    if (payload_len as u32) >= MAX_MESSAGE_LEN {
                        // message would be too big
                        return Err(net_error::DeserializeError(format!(
                            "Preamble payload length {} is too big",
                            payload_len
                        )));
                    }
```
