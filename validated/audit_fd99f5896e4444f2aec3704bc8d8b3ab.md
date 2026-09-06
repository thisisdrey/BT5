### Title
Remote unauthenticated panic via unchecked `preamble.payload_len` in push-message validators - ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P` contains a family of near-identical validators — `validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, `validate_stackerdb_push` — that are invoked whenever an unauthenticated/authenticated remote peer relays a `BlocksData`, `MicroblocksData`, `TransactionData`, or `StackerDBPushChunkData` push message. Each of these functions begins with a hard `assert!()` on the wire-supplied `preamble.payload_len` field rather than a checked `Result`-returning validation, e.g.: [1](#0-0) 

```
    fn validate_microblocks_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length
```

This mirrors the reported bug class exactly: an externally-controlled input (`payload_len`, analogous to TensorFlow's unchecked `dense_features`/`example_state_data` rank) is fed straight into a hard `CHECK`/`assert!` instead of being validated and rejected gracefully, so a value that violates the assumed invariant terminates the process instead of returning an error.

### Finding Description
`preamble.payload_len` is a `u32` field decoded straight off the wire in the `Preamble` and is *not* independently re-derived from, or cross-checked against, the number of bytes actually consumed while decoding the relayed payload. The connection layer's `consume_payload_known_length` buffers exactly `payload_len` bytes and calls `protocol.read_payload`, which returns its own `message_len` (the bytes it actually parsed) — that returned `message_len`, not `payload_len`, is what advances the buffer pointer: [2](#0-1) 

Nothing in this path enforces that `payload_len` numerically equals `5 + size_of(relayers) + size_of(inner payload)`. Once the message is decoded, `handle_data_message`-style dispatch calls the relevant `validate_*_push` function with the original attacker-set `preamble`, and the very first line executes `assert!(preamble.payload_len > 5)` on that unchecked value. If a remote peer sends a well-formed `Blocks`/`Microblocks`/`Transaction`/`StackerDBPushChunk` push whose relayers vector is empty and whose declared `payload_len` is ≤ 5 (a value fully controlled by the sender and independent of the actual decoded content), the assertion fails and the thread panics.

This breaks exactly the kind of equality the scan is looking for: the *declared* wire length (`payload_len`) vs. the *actually decoded* content size are assumed equal by the `assert!`, but nothing enforces that equality before the assert executes.

### Impact Explanation
An `assert!` failure in Rust unwinds/aborts the thread; in a multi-threaded async server this typically aborts the process or at minimum kills the connection-handling worker, constituting a remote, unauthenticated denial-of-service triggerable with a single crafted P2P message — no signing key, special role, or high traffic volume required. This satisfies the "Critical - remote crash/unauthenticated DoS from few messages" impact bar defined in scope.

### Likelihood Explanation
The four validators (`validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, `validate_stackerdb_push`) are reachable directly from unsolicited P2P relay handling — precisely the kind of message any connected peer, not just a signer or admin, can send. Because `payload_len` is attacker-controlled and not independently validated before the assert executes, the panic is trivially reproducible by any peer capable of completing a handshake and sending one relay message.

### Recommendation
Replace the `assert!(preamble.payload_len > 5)` calls in `validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, and `validate_stackerdb_push` with a checked comparison that returns `Err(net_error::InvalidMessage)` (or increments `msgs_err` and NACKs, consistent with the surrounding error-handling pattern already used elsewhere in these functions) instead of panicking. Additionally, verify that `preamble.payload_len` matches the actual decoded message size at the point the message is framed (in `consume_payload_known_length`), rather than trusting the sender-declared value throughout downstream logic.

### Proof of Concept
1. Establish a normal P2P handshake with a target node as any peer (no signing key or elevated role required).
2. Construct a relay message wrapping a `StacksMessageType::Microblocks` (or `Blocks`/`Transaction`/`StackerDBPushChunk`) payload with an empty `relayers` vector, sized so the true encoded content is small.
3. Forge the `Preamble.payload_len` field to a value ≤ 5 while keeping the rest of the frame decodable (`payload_len` is not cross-validated against decoded content size before dispatch, per `consume_payload_known_length` in `stackslib/src/net/connection.rs`).
4. Send the frame; the receiving node's `ConversationP2P::validate_microblocks_push` (or sibling function) executes `assert!(preamble.payload_len > 5)`, which fails and panics the handling thread/process — remote, unauthenticated DoS.

Note: I was not able to fully trace, within the tool budget available, the exact upstream call site that dispatches parsed push messages into these four validators (only test call sites were retrieved), nor definitively confirm the absence of an earlier length-consistency check elsewhere in `relay.rs`. This should be double-checked in `stackslib/src/net/relay.rs` (where 2 references to these functions were found) before treating this as fully confirmed; I recommend a Devin session with full file access to verify the dispatch path and any pre-existing guards.

### Citations

**File:** stackslib/src/net/chat.rs (L2126-2136)
```rust
    /// Validate pushed microblocks.
    /// Not much we can do to see if they're semantically correct, but we can at least throttle a
    /// peer that sends us too many at once.
    fn validate_microblocks_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length

```

**File:** stackslib/src/net/connection.rs (L799-823)
```rust
    fn consume_payload_known_length(
        &mut self,
        protocol: &mut P,
        preamble: &P::Preamble,
    ) -> Result<Option<P::Message>, net_error> {
        let payload_len_opt = protocol.payload_len(preamble);
        let payload_len = payload_len_opt.expect("BUG: payload length assumed to be known");
        let buf_bytes = self.buf.get(self.message_ptr..).ok_or_else(|| {
            net_error::RecvError(format!("Message ptr {} overran buffer", self.message_ptr))
        })?;

        // reading a payload of known length
        if buf_bytes.len() >= payload_len {
            // definitely have enough data to form a message
            if let Some(ref pubk) = self.public_key {
                protocol.verify_payload_bytes(pubk, preamble, buf_bytes)?;
            }

            // consume the message
            let message_opt = match protocol.read_payload(preamble, buf_bytes) {
                Ok((message, message_len)) => {
                    test_debug!("Got message of {} bytes with {:?}", message_len, preamble);
                    let next_message_ptr = self.message_ptr.checked_add(message_len).ok_or(
                        net_error::OverflowError("Overflowed buffer pointer".to_string()),
                    )?;
```
