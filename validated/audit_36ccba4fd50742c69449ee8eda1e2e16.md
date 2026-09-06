### Title
`Preamble.payload_len` is never checked against actual bytes consumed by `read_payload`, allowing P2P frame desynchronization / message smuggling - (File: `stackslib/src/net/codec.rs`, `stackslib/src/net/connection.rs`)

### Summary
`StacksP2P::read_payload` slices exactly `preamble.payload_len` bytes off the wire, but returns `cursor.position()` (the number of bytes actually consumed by `StacksMessage::deserialize_body`) as the "message length" used to advance the stream pointer, with no check that the two values are equal. `NetworkConnection::consume_payload_known_length` in `connection.rs` blindly advances `next_message_ptr` by this (potentially smaller) value, leaving unconsumed bytes inside the declared `payload_len` window in the buffer, where they are then reinterpreted as the start of the next `Preamble`.

### Finding Description
The broken equality is: `bytes consumed by deserialize_body` (`cursor.position()`) is assumed to equal `preamble.payload_len`, but this is never enforced.

- `Preamble::consensus_deserialize` only validates `payload_len` bounds (`>=5`, `< MAX_MESSAGE_LEN`); it does not, and cannot, know the true content length [1](#0-0) .
- `StacksP2P::read_payload` slices `bytes.get(..preamble.payload_len as usize)`, wraps it in a `Cursor`, calls `StacksMessage::deserialize_body` (which parses `Vec<RelayData>` then a `StacksMessageType`), and returns `cursor.position()` as the consumed length — without asserting `cursor.position() == preamble.payload_len` [2](#0-1) .
- `NetworkConnection::consume_payload_known_length` uses this returned `message_len` (not `payload_len`) to compute `next_message_ptr` and re-slices `self.buf` from there, treating any leftover bytes inside the claimed `payload_len` window as the start of the next message [3](#0-2) .
- The only integrity check on the raw bytes, `verify_payload_bytes`, is gated on `if let Some(ref pubk) = self.public_key` [4](#0-3) . For any connection where the peer's public key is not yet known (e.g., before a handshake reveals it), this check is skipped entirely, so a fully unauthenticated remote party can send a `Preamble` whose `payload_len` overstates the real encoded `relayers`+`payload` size, padding it with attacker-chosen trailing bytes.

Because `deserialize_body`'s internal length-prefixed fields (`Vec<RelayData>` length, payload type tag/size) determine how much of the `payload_len`-byte window is actually consumed, an attacker can make `cursor.position() < payload_len` by appending extra garbage after a validly-formed short message. The trailing garbage then becomes the byte offset from which the next `Preamble` is parsed, letting the attacker inject bytes that get reinterpreted as a different message than what was actually delimited by the sender's stated framing.

### Impact Explanation
This is a wire-framing desynchronization within the P2P protocol: an attacker can cause the peer's message loop to misalign frame boundaries, causing subsequent legitimate/attacker bytes to be misparsed as a bogus `Preamble`+message. Depending on downstream handling this can corrupt the peer's connection state (leading to spurious `InvalidMessage`/disconnects) or, in the pre-handshake/no-pubkey case, be triggered by a fully unauthenticated remote connection with no signature required — matching the "request smuggling ... across the P2P framing boundary" Critical category. This affects any single P2P-connected node, is repeatable per-connection, and requires no privileged role, secret, or valid peer signature when `self.public_key` is `None`.

### Likelihood Explanation
- The port (P2P listener) is remotely reachable by any peer that can open a TCP connection to the node — no privileged role, secret, or established handshake is required to reach `consume_messages`/`consume_payload_known_length`.
- The exploit is trivially cheap: craft a single `Preamble` with `payload_len` set larger than the actual serialized `relayers`+`payload`, and append arbitrary trailing bytes within that declared window.
- Precondition affecting severity: if the peer's public key is already known (post-handshake), `verify_payload_bytes` will check a signature over the full `payload_len`-byte slice, so only a peer capable of producing that signature (a legitimate, currently-connected counterparty) could exploit it after handshake; but pre-handshake / no-pubkey connections skip this check entirely, making it exploitable by a first-contact anonymous remote party.

### Recommendation
In `stackslib/src/net/codec.rs::StacksP2P::read_payload`, after `deserialize_body` completes, assert that `cursor.position() as usize == preamble.payload_len as usize`, and return `codec_error::DeserializeError`/`net_error::InvalidMessage` if there is a mismatch (i.e., require the payload to consume exactly the declared length, rejecting both under-consumption and any trailing bytes). Apply the same "exact consumption" check regardless of whether `self.public_key` is set, so unauthenticated connections are also protected.

### Proof of Concept
Rust test in `stackslib::net::codec::test`:
1. Build a valid short `StacksMessageType` payload (e.g., `Ping`) and serialize `relayers` (`empty vec`) + `payload` into `real_bytes`.
2. Construct a `Preamble` via `Preamble::new(...)`, set `payload_len = real_bytes.len() as u32 + K` (K > 0 extra padding bytes), sign it over `message_bits = real_bytes + garbage_bytes` using `Preamble::sign`.
3. Serialize `preamble` + `message_bits` (including garbage) into a byte buffer, followed by a second, distinct, valid `Preamble` (`preamble2`) to represent the "true" next message.
4. Feed the combined buffer through `StacksP2P::read_payload(&preamble, &buf[preamble_encoded_size..])` (or through `NetworkConnection`/`ConnectionInbox::consume_messages`).
5. Assert that `cursor.position()` (or the message length returned) is less than `preamble.payload_len`, and that the subsequent bytes parsed as the "next preamble" are actually the garbage padding rather than `preamble2`'s true encoding — demonstrating the desync. With the fix applied, the same test should assert `net_error::InvalidMessage` (or equivalent deserialize error) is returned instead.

### Citations

**File:** stackslib/src/net/codec.rs (L168-185)
```rust
        let payload_len: u32 = read_next(fd)?;

        // minimum is 5 bytes -- a zero-length vector (4 bytes of 0) plus a type identifier (1 byte)
        if payload_len < 5 {
            test_debug!("Payload len is too small: {}", payload_len);
            return Err(codec_error::DeserializeError(format!(
                "Payload len is too small: {}",
                payload_len
            )));
        }

        if payload_len >= MAX_MESSAGE_LEN {
            test_debug!("Payload len is too big: {}", payload_len);
            return Err(codec_error::DeserializeError(format!(
                "Payload len is too big: {}",
                payload_len
            )));
        }
```

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

**File:** stackslib/src/net/connection.rs (L811-815)
```rust
        if buf_bytes.len() >= payload_len {
            // definitely have enough data to form a message
            if let Some(ref pubk) = self.public_key {
                protocol.verify_payload_bytes(pubk, preamble, buf_bytes)?;
            }
```

**File:** stackslib/src/net/connection.rs (L817-843)
```rust
            // consume the message
            let message_opt = match protocol.read_payload(preamble, buf_bytes) {
                Ok((message, message_len)) => {
                    test_debug!("Got message of {} bytes with {:?}", message_len, preamble);
                    let next_message_ptr = self.message_ptr.checked_add(message_len).ok_or(
                        net_error::OverflowError("Overflowed buffer pointer".to_string()),
                    )?;

                    // begin parsing at the end of this message
                    let data = self.buf.get(next_message_ptr..).ok_or_else(|| {
                        net_error::RecvError(format!(
                            "Next message ptr {next_message_ptr} overran buffer"
                        ))
                    })?;

                    self.message_ptr = 0;
                    self.payload_ptr = 0;
                    self.buf = data.to_vec();

                    if !self.buf.is_empty() {
                        test_debug!(
                            "Buffer has {} bytes remaining: {:?}",
                            self.buf.len(),
                            &self.buf.to_vec()
                        );
                    }
                    Some(message)
```
