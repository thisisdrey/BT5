### Title
Remote panic (assert failure) in P2P push-message bandwidth accounting on minimal-length `Blocks`/`Microblocks` payloads - (File: `stackslib/src/net/chat.rs`)

### Summary
`ConversationP2P::validate_blocks_push` and `ConversationP2P::validate_microblocks_push` unconditionally assert that the peer-supplied `Preamble.payload_len` is strictly greater than 5 before subtracting 5 from it to update bandwidth-accounting counters. This value is derived directly from the wire and is not clamped/validated against a minimum before reaching the assert, so a message whose real (and self-consistent) serialized payload is exactly 5 bytes or fewer causes a Rust panic rather than a graceful protocol error, breaking the "well-formed-message accepted, malformed-message rejected gracefully" equality that the rest of the P2P dispatcher otherwise upholds.

### Finding Description
`validate_blocks_push` and `validate_microblocks_push` begin with: [1](#0-0) [2](#0-1) 

```rust
assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length
```

immediately followed later by: [3](#0-2) [4](#0-3) 

```rust
self.stats.add_block_push((preamble.payload_len as u64) - 5);
...
self.stats.add_microblocks_push((preamble.payload_len as u64) - 5);
```

`preamble.payload_len` is a `u32` field of the `Preamble`, filled in directly from message bytes received over the wire (see `Preamble` fields/constructors in `stackslib/src/net/codec.rs`), and it is only used to bound how many payload bytes are read for the enclosed `StacksMessageType`; it is not otherwise checked against a minimum length before dispatch reaches `validate_blocks_push`/`validate_microblocks_push`. The comment "don't count 1-byte type prefix + 4 byte vector length" documents the assumed invariant: for a `Blocks`/`Microblocks` push, the minimum possible encoded payload is exactly 5 bytes (1-byte type tag + 4-byte zero-length vector count for an *empty* block/microblock list). Since an empty vector is a syntactically valid encoding that the generic codec (`write_next`/`read_next`) will happily serialize/deserialize, a remote peer can construct — and have accepted through full message parsing — a `StacksMessageType::Blocks` (or `Microblocks`) push whose true payload length equals exactly 5 (or, via a truncated/malformed variant, less). Because the assert requires strictly greater than 5, this specific, otherwise well-formed minimal message triggers `assert!` failure, i.e. a Rust panic, instead of being rejected through the normal `net_error`/NACK path used everywhere else in this same file (e.g. `process_relayers` failures return `Err(net_error::InvalidMessage)` rather than panicking).

This differs from the analogous `transaction_push`/`stackerdb_push`/`nakamoto_block_push` validators, whose minimum encodings (opaque byte blobs rather than length-prefixed vectors) make it harder to hit `payload_len <= 1`, but the `Blocks`/`Microblocks` cases have a directly reachable, deterministic zero-length-vector construction.

### Impact Explanation
This is a remote, unauthenticated-relative-to-content DoS: any peer that has completed the ordinary (non-privileged) P2P handshake — which any node can do without possessing any secret beyond its own ephemeral keypair — can send a single crafted `Blocks` or `Microblocks` push message and crash the connection-handling thread of the victim node via a Rust `assert!` panic. Depending on the panic-handling configuration of the binary (`panic = "abort"` vs. unwind-and-catch), this can escalate from killing a single connection thread to aborting the whole node process. This falls under "Critical - remote crash/unauthenticated DoS from few messages" in the given severity taxonomy, since no admin role or victim secret is required — only a normal P2P connection.

### Likelihood Explanation
High. The construction requires no cryptographic material besides the attacker's own already-generated node keypair (used to sign their own legitimate outbound messages, as with any P2P participant), and the message itself is a small, deterministic single packet (an empty-vector `BlocksData`/`MicroblocksData` push). The relayer list can be empty or minimal, and `process_relayers` is called successfully before the vulnerable assert is reached, so the panic path is directly reachable in the normal dispatch flow for pushed blocks/microblocks (`handle_data_message` → `validate_blocks_push`/`validate_microblocks_push`).

### Recommendation
Replace the `assert!` invariant checks in `validate_blocks_push` and `validate_microblocks_push` with a graceful error path (return `Err(net_error::InvalidMessage)` or send a NACK), mirroring how `process_relayers` failures are already handled, rather than panicking on peer-supplied `payload_len`. Additionally, perform an explicit bounds check (`payload_len` `<=` `5` ⇒ reject) prior to the subtraction so the accounting subtraction can no longer be paired with a hard process-level assertion on attacker-controlled input.

### Proof of Concept
1. Complete a normal (unprivileged) P2P handshake with the target node as any peer.
2. Construct a `StacksMessage` whose payload is `StacksMessageType::Blocks(BlocksData { blocks: vec![] })` (or `Microblocks` with an empty microblock vector), sign it as required by the protocol's message-signing procedure (any peer can sign its own messages with its own key), and set the (empty) relayers list so `process_relayers` succeeds.
3. Send this message to the target over an established P2P connection.
4. The target's `ConversationP2P` dispatch invokes `validate_blocks_push`/`validate_microblocks_push`; because `preamble.payload_len` for this minimal message equals exactly 5, the `assert!(preamble.payload_len > 5)` check fails, causing a panic on the receiving node's connection-processing thread/process.

### Citations

**File:** stackslib/src/net/chat.rs (L2094-2096)
```rust
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length

```

**File:** stackslib/src/net/chat.rs (L2106-2106)
```rust
        self.stats.add_block_push((preamble.payload_len as u64) - 5);
```

**File:** stackslib/src/net/chat.rs (L2134-2136)
```rust
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length

```

**File:** stackslib/src/net/chat.rs (L2149-2150)
```rust
        self.stats
            .add_microblocks_push((preamble.payload_len as u64) - 5);
```
