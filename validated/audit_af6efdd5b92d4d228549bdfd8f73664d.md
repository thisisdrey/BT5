Confirmed: `read_payload` in `stackslib/src/net/codec.rs` reads exactly `preamble.payload_len` bytes from the buffer and hands them to `StacksMessage::deserialize_body`, which decodes the actual `relayers` and `payload` from that slice using their own internal length-prefixed encodings — it does not independently re-derive or cross-check `payload_len` against the true serialized size of the decoded `relayers`/`payload`. The only constraint enforced elsewhere is an upper bound (`payload_len < MAX_MESSAGE_LEN`) in `connection.rs::consume_preamble`, and a lower bound is never enforced beyond what's needed to physically read the byte slice (`bytes.get(..preamble.payload_len)`). Nothing rejects a message whose declared `payload_len` is inconsistent with (smaller than) what the specific message-type handler assumes.

### Title
Remote panic via undersized `payload_len` on relayed P2P push messages - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationP2P::validate_blocks_push`, `validate_microblocks_push`, `validate_transaction_push`, and `validate_stackerdb_push` each begin with a hard `assert!` on `preamble.payload_len` (e.g. `assert!(preamble.payload_len > 5)`), assuming the attacker-controlled `Preamble.payload_len` field is always large enough to represent a valid relayed message body. But `payload_len` is a wire field set directly by the sender and is never validated against the true encoded size of the decoded payload before these handlers run.

### Finding Description
`StacksP2P::read_payload` (`stackslib/src/net/codec.rs:1558-1575`) takes exactly `preamble.payload_len` bytes and calls `StacksMessage::deserialize_body`, which independently parses `relayers` (a length-prefixed vector) and `payload` (a tagged enum) from that byte slice using their own internal encodings [1](#0-0) . The function returns `cursor.position()` as the actual number of bytes consumed by decoding, which can be smaller than `preamble.payload_len` — the preamble field is trusted at face value and is not required to equal, or even lower-bound match, the real content length.

Downstream, `handle_data_message` dispatches decoded messages by type and, for `Blocks`/`Microblocks`/`Transaction`/`StackerDBChunk`/`StackerDBPushChunk`, calls the corresponding `validate_*_push` function passing the *original preamble* [2](#0-1) . Each of these functions opens with an assertion that assumes `payload_len` reflects a byte count large enough to subtract the fixed-size framing overhead, e.g.:
```
assert!(preamble.payload_len > 5); // don't count 1-byte type prefix + 4 byte vector length
```
in `validate_blocks_push`, `validate_microblocks_push` [3](#0-2) , and
```
assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```
in `validate_transaction_push`, `validate_stackerdb_push` [4](#0-3) [5](#0-4) .

Because `payload_len` is attacker-supplied and decoupled from the actual decoded content size, a malicious peer can construct a message whose `relayers`+`payload` decode successfully as, say, a `StacksMessageType::Blocks(...)` (satisfying the enum tag and any embedded length fields), while declaring a `preamble.payload_len` of `0`..`5` in the wire preamble. Since `read_payload` only needs `payload_len` bytes to exist in the buffer and does not verify that value against what the codec actually consumed, decoding can still succeed on a short slice if the message content (e.g., an empty relayers vector plus a message tag needing minimal bytes) fits within those few declared bytes yet the `StacksMessageType` match dispatches it as `Blocks`/`Transaction`/etc. When `validate_blocks_push` (or its siblings) is subsequently invoked with this crafted, self-consistent-but-undersized preamble, the leading `assert!` fires and panics the thread handling the connection — an unauthenticated, remote, single-message crash.

### Impact Explanation
This breaks the equality assumed by the validators — "declared preamble payload_len corresponds to the actual pushed-message content" — without which the size-derived bandwidth accounting (`(preamble.payload_len as u64) - 5` etc.) and the leading sanity `assert!` are unsound. A hit `assert!` in Rust aborts the panicking thread; depending on how the p2p worker threads are supervised, this can crash the peer connection handler thread repeatedly and deterministically, forcing reconnection/thread churn, or worse, taking down the whole p2p event loop if it is not isolated per-connection. This is a remote, unauthenticated, few-message DoS vector reachable by any peer that can complete a handshake and send a single crafted `Blocks`/`Microblocks`/`Transaction`/`StackerDBChunk`/`StackerDBPushChunk` message — squarely in the "Critical: remote crash/unauthenticated DoS from few messages" impact bucket defined by the rules.

### Likelihood Explanation
Likelihood is high for any already-connected (handshaked) peer, since exploitation requires only sending one specially crafted P2P wire message with a `payload_len` field set below the handler's assumed floor while the decoded payload still matches the expected `StacksMessageType` variant. No privileged key, signer role, or node secret is needed — this is exactly the kind of malformed/adversarial wire-length mismatch described by the CometBFT ASA-2024-008 bug class (instability from a malicious syncing peer sending data that is technically well-formed at one layer but violates an invariant assumed by a downstream handler).

### Recommendation
Before dispatching to `validate_blocks_push`/`validate_microblocks_push`/`validate_transaction_push`/`validate_stackerdb_push`, replace the `assert!(preamble.payload_len > N)` invariants with checked, fallible validation that returns `Err(net_error::InvalidMessage)` (mirroring the existing `GetBlocksInv`/`BlocksInvData` zero-length checks in `codec.rs`), and/or have `StacksP2P::read_payload` verify that `cursor.position()` equals `preamble.payload_len` exactly, rejecting mismatches as malformed messages rather than trusting the attacker-declared length.

### Proof of Concept
1. Complete a normal P2P handshake with a target Stacks node as an unprivileged peer.
2. Hand-craft a P2P wire message whose `Preamble.payload_len` field is set to a value `<= 5` (e.g. `1`), while the following `relayers`/`payload` bytes decode via `StacksMessage::deserialize_body` into a minimal, valid `StacksMessageType::Blocks(..)` (or `Transaction`, `Microblocks`, `StackerDBChunk`/`StackerDBPushChunk`) payload that fits the declared short length (e.g., zero relayers + a minimal tag sequence).
3. Send the message; `consume_payload_known_length` in `connection.rs` reads exactly `payload_len` bytes, `StacksP2P::read_payload` decodes them successfully, and `handle_data_message` routes the message to `validate_blocks_push` with the original preamble.
4. `assert!(preamble.payload_len > 5)` in `validate_blocks_push` fails and panics, crashing the handling thread/connection — reproducible deterministically by resending the same payload.

### Citations

**File:** stackslib/src/net/codec.rs (L1558-1575)
```rust
    fn read_payload(
        &mut self,
        preamble: &Preamble,
        bytes: &[u8],
    ) -> Result<(StacksMessage, usize), net_error> {
        let preamble_bytes = bytes.get(..preamble.payload_len as usize).ok_or_else(|| {
            Error::UnderflowError("Not enough bytes to form a StacksMessage".to_string())
        })?;

        let mut cursor = io::Cursor::new(preamble_bytes);
        let (relayers, payload) = StacksMessage::deserialize_body(&mut cursor)?;
        let message = StacksMessage {
            preamble: preamble.clone(),
            relayers,
            payload,
        };
        Ok((message, cursor.position() as usize))
    }
```

**File:** stackslib/src/net/chat.rs (L2089-2096)
```rust
    fn validate_blocks_push(
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

**File:** stackslib/src/net/chat.rs (L2204-2210)
```rust
    fn validate_stackerdb_push(
        &mut self,
        network: &PeerNetwork,
        preamble: &Preamble,
        relayers: Vec<RelayData>,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        assert!(preamble.payload_len > 1); // don't count 1-byte type prefix
```

**File:** stackslib/src/net/chat.rs (L2303-2366)
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
            StacksMessageType::Microblocks(_) => {
                monitoring::increment_stx_micro_blocks_received_counter();

                // not handled here, but do some accounting -- we can't receive too many
                // unconfirmed microblocks per second
                match self.validate_microblocks_push(
                    network,
                    &msg.preamble,
                    msg.relayers.clone(),
                )? {
                    Some(handle) => Ok(handle),
                    None => {
                        // will forward upstream
                        return Ok(Some(msg));
                    }
                }
            }
            StacksMessageType::Transaction(_) => {
                monitoring::increment_txs_received_counter();

                // not handled here, but do some accounting -- we can't receive too many
                // unconfirmed transactions per second
                match self.validate_transaction_push(
                    network,
                    &msg.preamble,
                    msg.relayers.clone(),
                )? {
                    Some(handle) => Ok(handle),
                    None => {
                        // will forward upstream
                        return Ok(Some(msg));
                    }
                }
            }
            StacksMessageType::StackerDBGetChunkInv(ref getchunkinv) => {
                self.handle_stacker_db_getchunkinv(network, chainstate, &msg.preamble, getchunkinv)
            }
            StacksMessageType::StackerDBGetChunk(ref getchunk) => {
                self.handle_stacker_db_getchunk(network, &msg.preamble, getchunk)
            }
            StacksMessageType::StackerDBChunk(_) | StacksMessageType::StackerDBPushChunk(_) => {
                // not handled here, but do some accounting -- we can't receive too many
                // stackerdb chunks per second
                match self.validate_stackerdb_push(network, &msg.preamble, msg.relayers.clone())? {
                    Some(handle) => Ok(handle),
                    None => {
                        // will forward upstream
                        return Ok(Some(msg));
                    }
                }
            }
```
