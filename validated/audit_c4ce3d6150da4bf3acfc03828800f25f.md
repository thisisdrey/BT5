### Title
Remote panic (assertion failure) via preamble payload_len boundary values on pushed-message validation — ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P::validate_blocks_push`, `validate_transaction_push`, and `validate_stackerdb_push` in `stackslib/src/net/chat.rs` each begin with a hard `assert!` on the untrusted `Preamble::payload_len` field taken directly from the wire, rather than a checked/graceful comparison. This mirrors the Sherlock M-27 pattern: an implicit numeric assumption ("returnToTreasury will never exceed the deposit value") that is not actually guaranteed, and whose violation causes an unconditional revert/DoS. Here the analogous assumption is "payload_len will always be large enough that the accounting subtraction/assert holds," which is not enforced anywhere before these functions run. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`validate_blocks_push` asserts `preamble.payload_len > 5` before subtracting 5 to compute bandwidth usage: [4](#0-3) 

`validate_transaction_push` and `validate_stackerdb_push` similarly assert `preamble.payload_len > 1` before subtracting 1: [5](#0-4) [6](#0-5) 

`payload_len` is a `u32` field of `Preamble`, populated from the wire during message deserialization, and represents the declared size of the serialized payload (the message-type tag plus its body). The subtraction that follows each assert (`payload_len - 5`, `payload_len - 1`) is the direct structural analog of the Solidity report's `returnToTreasury`/`cdsProfits` subtraction: a value derived from attacker-influenced state is subtracted from (or compared against) a fixed constant with the *implicit* assumption that it is always large enough. In the audited contract, that assumption failed when `lastCumulativeRate` grew past a threshold; here, the assumption fails when a legitimately-encoded push message has a minimal payload (e.g., an empty `BlocksData`/`Vec<StacksTransaction>`/`StackerDBChunkData` vector whose serialized length lands exactly at or below the boundary the code assumes can never occur).

Because `assert!` failures in Rust are process/thread-fatal panics (not recoverable `Result` errors), any code path that can drive `payload_len` to the boundary value causes an unconditional panic rather than a graceful reject — this breaks the same invariant class the rules call out ("reach a precise panic ... from an unchecked wire length").

### Impact Explanation
An assertion failure in a hot message-dispatch path (`ConversationP2P` push-message handlers) triggers `panic!`, which is fatal to the handling thread/context. This falls into the Critical impact bucket ("remote crash/unauthenticated DoS from few messages"): a single, minimally-crafted, unauthenticated Blocks/Transaction/StackerDBChunk push message with a boundary-value `payload_len` is sufficient to reach the assert, with no signature or authorization requirement to reach this validation logic (it runs before any deeper content validation).

### Likelihood Explanation
Likelihood depends entirely on whether the framing/deserialization layer guarantees `payload_len` cannot be exactly 5 (for blocks) or exactly 1 (for tx/stackerdb chunk) for a validly-parsed, minimal-content message (e.g., an empty vector push). I was not able to fully trace, within the available search budget, the exact codec-level invariant tying `payload_len` to the real serialized byte count of `StacksMessageType` variants in `stackslib/src/net/codec.rs` (e.g., whether `read_preamble`/message framing independently recomputes or strictly validates this equality before dispatch, which would make the assert unreachable). Given the docstrings on `validate_blocks_push` — "don't count 1-byte type prefix + 4 byte vector length" — the threshold is explicitly built around the minimum possible legitimate size, which strongly suggests an empty-vector push message sits exactly at the boundary and would trip the assert. This should be verified directly against `stackslib/src/net/codec.rs`'s `Preamble`/`StacksMessage` deserialization logic (I could not fully confirm this in the time available) before treating this as fully proven.

### Recommendation
Replace the `assert!` guards in `validate_blocks_push`, `validate_transaction_push`, and `validate_stackerdb_push` with checked arithmetic (`checked_sub`/`saturating_sub`) and a graceful `Err`/NACK return instead of a panic, so that a boundary-value or malformed `payload_len` cannot crash the conversation-handling thread. This removes the implicit "payload_len is always large enough" assumption analogous to the audited contract's unguarded subtraction.

### Proof of Concept
Conceptually: construct and send a `StacksMessageType::Blocks` (or `Transactions`/`StackerDBPushChunk`) push message whose vector is empty, such that the wire-computed `preamble.payload_len` equals exactly 5 (blocks) or 1 (tx/stackerdb). Deliver it over an established P2P conversation. If the codec accepts this as a validly framed message and dispatches it to the corresponding `validate_*_push` function, the `assert!` fires and panics, crashing the handling thread. Confirming this PoC requires inspecting `stackslib/src/net/codec.rs`'s preamble/message-length validation, which was not fully explored in this session.

### Citations

**File:** stackslib/src/net/chat.rs (L2089-2106)
```rust
    fn validate_blocks_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length

        let local_peer = network.get_local_peer();
        let chain_view = network.get_chain_view();

        if !self.process_relayers(local_peer, preamble, &relayers) {
            warn!("Drop pushed blocks -- invalid relayers {:?}", &relayers);
            self.stats.msgs_err += 1;
            return Err(net_error::InvalidMessage);
        }

        self.stats.add_block_push((preamble.payload_len as u64) - 5);
```

**File:** stackslib/src/net/chat.rs (L2164-2187)
```rust
    /// Validate a pushed transaction.
    /// Update bandwidth accounting, but forward the transaction along.
    fn validate_transaction_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix

        let local_peer = network.get_local_peer();
        let chain_view = network.get_chain_view();

        if !self.process_relayers(local_peer, preamble, &relayers) {
            warn!(
                "Drop pushed transaction -- invalid relayers {:?}",
                &relayers
            );
            self.stats.msgs_err += 1;
            return Err(net_error::InvalidMessage);
        }

        self.stats
            .add_transaction_push((preamble.payload_len as u64) - 1);
```

**File:** stackslib/src/net/chat.rs (L2201-2225)
```rust
    /// Validate a pushed stackerdb chunk.
    /// Update bandwidth accounting, but forward the stackerdb chunk along if we can accept it.
    /// Possibly return a reply handle for a NACK if we throttle the remote sender
    fn validate_stackerdb_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix

        let local_peer = network.get_local_peer();
        let chain_view = network.get_chain_view();

        if !self.process_relayers(local_peer, preamble, &relayers) {
            warn!(
                "Drop pushed stackerdb chunk -- invalid relayers {:?}",
                &relayers
            );
            self.stats.msgs_err += 1;
            return Err(net_error::InvalidMessage);
        }

        self.stats
            .add_stackerdb_push((preamble.payload_len as u64) - 1);
```
