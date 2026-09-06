### Title
Remote unauthenticated panic via attacker-controlled `preamble.payload_len` in push-message validators - (File: `stackslib/src/net/chat.rs`)

### Summary
Several inbound "push" message validators in `ConversationP2P` gate their bandwidth-accounting logic with a bare `assert!` on `preamble.payload_len`, a value that is taken directly from the wire-supplied `Preamble` and is not cross-checked against the actual size of the deserialized payload before the assertion runs. A remote, unauthenticated peer that sends a `StackerDBPushChunk` (or `Transaction`, `Blocks`, `Microblocks`, `NakamotoBlocks`) message whose `Preamble.payload_len` field is set to `0` or `1` (or `<=5` for microblocks) can trigger the assertion and panic the connection-handling code path.

### Finding Description
`validate_stackerdb_push`, `validate_transaction_push`, `validate_nakamoto_block_push`, `validate_blocks_push`, and `validate_microblocks_push` in `stackslib/src/net/chat.rs` each begin with a hard assertion instead of a checked/returned error: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

`preamble.payload_len` is a field of the `Preamble` struct that is deserialized straight from wire bytes sent by the peer before the specific message body (`StackerDBPushChunkData`, `TransactionData`, etc.) is decoded. The message body's own internal framing (its length-prefixed fields) determines how the payload is actually parsed; `payload_len` is only consulted afterwards, in these validators, for bandwidth-throttling bookkeeping (`self.stats.add_stackerdb_push((preamble.payload_len as u64) - 1)`), not as a bound on deserialization. This is exactly the type of bug class described in the M-13 report analog: an "equality" between two independently-derived quantities (declared wire length vs. actual/expected payload size) is assumed to hold but is never enforced before being relied upon — here relied upon by a subtraction that must not underflow and by an `assert!` that must not fail.

Because `assert!` (not `debug_assert!`) is used, the check is compiled into release binaries. If a remote peer can cause the message type dispatch to reach one of these validators while supplying a `payload_len` of `0` or `1` (or `<= 5` for the microblock push variant), the `assert!` fails and the thread executing this code path panics.

### Impact Explanation
An assertion failure in a P2P message-handling routine that is reachable by any unauthenticated remote peer able to open a P2P connection is a crash-inducing bug. Depending on how panics propagate in this thread model, this can tear down the connection-handling thread or, if unwinding is not isolated, crash the node process. This matches the Critical bar in the rubric: "remote crash/unauthenticated DoS from few messages." No authentication, valid StackerDB signer key, or privileged role is required — only the ability to open a P2P connection and send one crafted push message.

### Likelihood Explanation
Reaching the vulnerable code requires only crafting a `Preamble` with an attacker-chosen `payload_len` value alongside a push-type message body (`StackerDBPushChunk`, `Transaction`, `Blocks`, `Microblocks`, or `NakamotoBlocks`) that would otherwise decode successfully. Because `payload_len` is not independently verified against the decoded body size anywhere prior to these validators (based on the code paths inspected), likelihood is assessed as plausible, though full confirmation would require also tracing the exact caller (`handle_data_message`/message dispatch in `chat.rs`) to rule out any earlier length-consistency check on `preamble.payload_len` — I was not able to fully confirm the absence of such a check before the tool budget ran out, so this should be verified with a live PoC before being treated as fully confirmed.

### Recommendation
Replace the `assert!(preamble.payload_len > N)` checks in `validate_stackerdb_push`, `validate_transaction_push`, `validate_blocks_push`, `validate_microblocks_push`, and `validate_nakamoto_block_push` with checked comparisons that return `Err(net_error::InvalidMessage)` (or issue a NACK) instead of panicking, and additionally verify `payload_len` is consistent with the actual serialized size of the received payload before it is used in any arithmetic (to also prevent an underflow in the `(payload_len as u64) - N` subtraction).

### Proof of Concept
1. Establish a P2P connection to a target node as an unauthenticated peer (standard handshake).
2. Send a `StacksMessage` whose `Preamble.payload_len` field is set to `0` (or `1`), but whose message-type byte and body correspond to a `StackerDBPushChunk` (or `Transaction`/`Blocks`/`Microblocks`/`NakamotoBlocks`) message that otherwise decodes without error via its own internal length framing.
3. When the node's relayer dispatches this message to `validate_stackerdb_push` (or the sibling validators), the `assert!(preamble.payload_len > 1)` (or `> 5` for microblocks) fails, panicking the handling thread.

Full verification of the exact call path from message-dispatch to these validators, and confirmation that no earlier length-consistency check exists, was not completed due to tool-call limits — this should be confirmed with a live test against `stackslib/src/net/chat.rs`'s message dispatch logic before treating this as fully proven.

### Citations

**File:** stackslib/src/net/chat.rs (L2129-2135)
```rust
    fn validate_microblocks_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length
```

**File:** stackslib/src/net/chat.rs (L2166-2172)
```rust
    fn validate_transaction_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```

**File:** stackslib/src/net/chat.rs (L2204-2211)
```rust
    fn validate_stackerdb_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix

```

**File:** stackslib/src/net/chat.rs (L2243-2249)
```rust
    fn validate_nakamoto_block_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```
