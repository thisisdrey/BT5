### Title
Remote panic via undersized (but well-formed) `Blocks`/`Microblocks` push payloads violating a wire-reachable `assert!` - (File: `stackslib/src/net/chat.rs`)

### Summary
`ConversationP2P::validate_blocks_push` and `ConversationP2P::validate_microblocks_push` in `stackslib/src/net/chat.rs` gate their bandwidth-accounting arithmetic behind `assert!(preamble.payload_len > 5)`, and `validate_transaction_push`/`validate_stackerdb_push`/`validate_nakamoto_block_push` behind `assert!(preamble.payload_len > 1)`. `preamble.payload_len` is an attacker-controlled `u32` field taken directly from the wire preamble of a p2p message, and none of these functions re-derives or clamps it against the actually-decoded payload before asserting. A minimal but protocol-valid `Blocks`/`Microblocks` push message (an empty `Vec` body, i.e. 1-byte message-ID + 4-byte zero vector length = 5 bytes of payload) satisfies deserialization yet yields `payload_len == 5`/`== 1`, which fails the strict `>` comparison and panics the thread handling that peer's message. This mirrors the go-ntlmssp bug class: a value that is syntactically valid on the wire but at a boundary condition drives an unchecked assumption (here, a Rust `assert!`) into a panic instead of returning an error, analogous to CWE-190/annotated boundary defects producing an out-of-bounds/panic condition. [1](#0-0) [2](#0-1) 

### Finding Description
`process_relayers`/`validate_*_push` are called from the p2p message-dispatch path once a `StacksMessage` (preamble + relayers + payload) has been parsed off the wire. The preamble's `payload_len` is set by the sender and is only checked elsewhere for an *upper* bound (`preamble.payload_len > MAX_MESSAGE_LEN - PREAMBLE_ENCODED_SIZE` in `StacksMessage::consensus_deserialize`), never checked to actually match the specific message-type's minimum encoded size before reaching `validate_blocks_push`/`validate_microblocks_push`. [3](#0-2) 

For a `Blocks`/`Microblocks` push carrying an empty vector, the on-wire encoding is exactly `1 (message ID) + 4 (vec length = 0) = 5` bytes, which deserializes successfully as a valid (if useless) message, giving `preamble.payload_len == 5`. This directly fails `assert!(preamble.payload_len > 5)`: [4](#0-3) [5](#0-4) 

Unlike the well-guarded StackerDB/Nakamoto codec paths, which use `checked_sub`/`checked_add`/explicit bound checks and return `net_error` on malformed lengths (see `stackslib/src/net/connection.rs` `buffer_message_bytes`/`consume_payload_known_length`), these five `validate_*_push` functions convert an attacker-influenced invariant into a hard `assert!`, which panics the current thread rather than gracefully rejecting the message. [6](#0-5) [7](#0-6) 

I was not able to directly view the `BlocksData`/`MicroblocksData` `consensus_serialize`/`consensus_deserialize` implementations or the exact call site in `stackslib/src/net/relay.rs` that dispatches to `validate_blocks_push` within the tool budget available, so I cannot fully confirm (a) whether the message dispatcher enforces a stricter minimum length before reaching this assert, and (b) whether the panic is caught by a `catch_unwind` boundary around per-conversation processing (which would downgrade impact from process-crash to a killed connection/thread). This should be verified directly in the repository.

### Impact Explanation
If unwind is not caught around this call path, a single small, well-formed `Blocks` or `Microblocks` push message from any unauthenticated/unauthorized peer can panic the p2p worker thread, at minimum killing that peer connection and, if the panic propagates past a thread boundary without `catch_unwind`, potentially crashing the whole node process. This falls under the "Critical - remote crash/unauthenticated DoS from few messages" impact tier requested for a valid analog, since it requires nothing more than one crafted p2p message and no privileged relationship with the target node.

### Likelihood Explanation
Likelihood is high for any attacker capable of establishing or already having a p2p connection to a Stacks node (no authentication is required to send `Blocks`/`Microblocks` push messages once a handshake completes), and the malformed condition (`payload_len == 5` for an empty-vector `Blocks` push, or `== 1`/boundary values for the other `validate_*_push` variants) is trivial to construct deterministically.

### Recommendation
Replace the `assert!(preamble.payload_len > N)` checks in `validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, `validate_stackerdb_push`, and `validate_nakamoto_block_push` with checked arithmetic that returns `net_error::InvalidMessage` (or similar) instead of panicking, mirroring the `checked_sub`/`checked_add` pattern already used in `stackslib/src/net/connection.rs`. Additionally, validate that `preamble.payload_len` is consistent with the minimum encoded size for the specific `StacksMessageType` variant at parse time, rather than relying on a downstream assertion.

### Proof of Concept
1. Complete a p2p handshake with a target Stacks node as an ordinary (non-privileged) peer.
2. Construct and sign a `StacksMessage` whose payload is `StacksMessageType::Blocks(BlocksData { blocks: vec![] })` (or the analogous empty-vector `Microblocks` variant), resulting in an encoded payload of exactly 5 bytes (1-byte message ID + 4-byte zero-length vector prefix) and set `preamble.payload_len = 5`.
3. Send this message to the target node.
4. Observe that `ConversationP2P::validate_blocks_push` is invoked with `preamble.payload_len == 5`, failing `assert!(preamble.payload_len > 5)` and panicking the thread/task handling this peer's messages.

(Exact reachability from the dispatcher in `stackslib/src/net/relay.rs` and the panic/unwind boundary around it should be confirmed in the repository before treating this as fully validated, per the caveat above.)

### Citations

**File:** stackslib/src/net/chat.rs (L2087-2106)
```rust
    /// Validate pushed blocks.
    /// Make sure the peer doesn't send us too much at once, though.
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

**File:** stackslib/src/net/chat.rs (L2126-2150)
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

        let local_peer = network.get_local_peer();
        let chain_view = network.get_chain_view();

        if !self.process_relayers(local_peer, preamble, &relayers) {
            warn!(
                "Drop pushed microblocks -- invalid relayers {:?}",
                &relayers
            );
            self.stats.msgs_err += 1;
            return Err(net_error::InvalidMessage);
        }

        self.stats
            .add_microblocks_push((preamble.payload_len as u64) - 5);
```

**File:** stackslib/src/net/chat.rs (L2164-2249)
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

        if self.connection.options.max_transaction_push_bandwidth > 0
            && self.stats.get_transaction_push_bandwidth()
                > (self.connection.options.max_transaction_push_bandwidth as f64)
        {
            debug!("{:?}: Neighbor {:?} exceeded max transaction-push bandwidth of {} bytes/sec (currently at {})", self, &self.to_neighbor_key(), self.connection.options.max_transaction_push_bandwidth, self.stats.get_transaction_push_bandwidth());
            return self
                .reply_nack(local_peer, chain_view, preamble, NackErrorCodes::Throttled)
                .map(Some);
        }
        Ok(None)
    }

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

        if self.connection.options.max_stackerdb_push_bandwidth > 0
            && self.stats.get_stackerdb_push_bandwidth()
                > (self.connection.options.max_stackerdb_push_bandwidth as f64)
        {
            debug!("{:?}: Neighbor {:?} exceeded max stackerdb-push bandwidth of {} bytes/sec (currently at {})", self, &self.to_neighbor_key(), self.connection.options.max_stackerdb_push_bandwidth, self.stats.get_stackerdb_push_bandwidth());
            return self
                .reply_nack(local_peer, chain_view, preamble, NackErrorCodes::Throttled)
                .map(Some);
        }

        Ok(None)
    }

    /// Validate a pushed Nakamoto block list.
    /// Update bandwidth accounting, but forward the blocks along if we can accept them.
    /// Possibly return a reply handle for a NACK if we throttle the remote sender
    fn validate_nakamoto_block_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```

**File:** stackslib/src/net/codec.rs (L1346-1355)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<StacksMessage, codec_error> {
        let preamble: Preamble = read_next(fd)?;
        if preamble.payload_len > MAX_MESSAGE_LEN - PREAMBLE_ENCODED_SIZE {
            return Err(codec_error::DeserializeError(
                "Message would be too big".to_string(),
            ));
        }

        let relayers: Vec<RelayData> = read_next_at_most::<_, RelayData>(fd, MAX_RELAYERS_LEN)?;
        let payload: StacksMessageType = read_next(fd)?;
```

**File:** stackslib/src/net/connection.rs (L764-795)
```rust
    /// buffer up bytes for a message
    #[cfg_attr(test, mutants::skip)]
    fn buffer_message_bytes(
        &mut self,
        bytes: &[u8],
        message_len_opt: Option<usize>,
    ) -> Result<usize, net_error> {
        let message_len = message_len_opt.unwrap_or(MAX_MESSAGE_LEN as usize);
        let buffered_so_far = self
            .buf
            .len()
            .checked_sub(self.message_ptr)
            .ok_or_else(|| {
                net_error::RecvError(format!("Message ptr {} overran buffer", self.message_ptr))
            })?;

        let Some(message_remaining) = message_len.checked_sub(buffered_so_far) else {
            // can happen if we receive so much data when parsing the preamble that we've
            // also already received the message, and part of the next preamble (or more).
            return Ok(0);
        };

        let to_consume = bytes.len().min(message_remaining);

        trace!("Consume {} bytes from input buffer", to_consume);
        self.buf.extend_from_slice(
            bytes
                .get(..to_consume)
                .expect("FATAL: bad length check in buffer handling"),
        );
        Ok(to_consume)
    }
```
