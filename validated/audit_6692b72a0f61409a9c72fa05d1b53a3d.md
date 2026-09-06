### Title
Message-framing desync via `message_len` vs `preamble.payload_len` mismatch enables P2P request smuggling - (File: stackslib/src/net/connection.rs)

### Summary
`ConnectionInbox::consume_payload_known_length` advances the stream pointer using `message_len`, the value returned by `protocol.read_payload()` (i.e. the number of bytes the inner deserializer actually consumed via `cursor.position()`), instead of `preamble.payload_len`, the value declared in and validated against the preamble. If any `StacksMessageType` payload variant can be deserialized while consuming fewer bytes than `payload_len` declares, the residual "padding" bytes the attacker placed after the true payload get reinterpreted as the start of the next message's preamble, desynchronizing the stream for every subsequent message on the connection.

### Finding Description
In `consume_payload_known_length` (stackslib/src/net/connection.rs:799-860), the buffered bytes are only checked with `buf_bytes.len() >= payload_len` (line 811) to ensure enough data has arrived — `buf_bytes` itself is never truncated to exactly `payload_len` before being handed to `protocol.read_payload(preamble, buf_bytes)` (line 818). The framer then computes:

```
let next_message_ptr = self.message_ptr.checked_add(message_len)...
``` [1](#0-0) 

where `message_len` is whatever the protocol's deserializer says it consumed (`cursor.position()`), not `payload_len` from the preamble. There is no assertion anywhere in this function that `message_len == payload_len`. `consume_preamble` (connection.rs:704-762) only bounds `payload_len` below `MAX_MESSAGE_LEN`; it performs no equality check with the number of bytes the payload deserializer actually consumes.

The equality that must hold for correct framing — bytes consumed by `StacksMessage`/`StacksMessageType` deserialization equals `preamble.payload_len` exactly — is never enforced in `connection.rs`. If any wire-format payload variant can be validly deserialized while consuming fewer bytes than declared in `payload_len` (e.g., an enum/type tag that short-circuits, or a variant whose body is shorter than its own declared/allowed length), the attacker can pad the remainder of the declared payload with bytes of their choosing. The framer will advance `next_message_ptr` only by the actual consumed length, leaving the attacker's padding bytes at the front of `self.buf` when it is reset (line 834: `self.buf = data.to_vec();`), and those bytes are then parsed as the next `Preamble`/message — request smuggling / stream desynchronization.

### Impact Explanation
An attacker who can complete the P2P handshake (or otherwise reach `consume_payload_known_length` on any message type) can smuggle attacker-chosen bytes into the position the peer's framer treats as the start of the next message. Depending on what fields land at the "preamble" offset, this can corrupt subsequent message parsing, cause messages to be misattributed/misrouted, or produce persistent desynchronization of the whole connection (every later message on that stream is parsed from the wrong offset) — matching the "request smuggling / auth bypass" Critical category.

### Likelihood Explanation
Preconditions: a single TCP connection to a node's P2P port in any conversation state, and the existence of at least one payload variant whose successful deserialization can consume fewer bytes than the preamble's declared `payload_len` while still satisfying `verify_payload_bytes`/`read_payload`. This requires no privileged role, no secret, and no chain state — only the ability to open a P2P connection and send bytes, which matches the assumed unprivileged remote attacker. Repeatable per-connection.

### Recommendation
In `consume_payload_known_length`, after calling `protocol.read_payload`, explicitly assert `message_len == payload_len` and reject the message (treat as `InvalidMessage`) if they differ, rather than trusting the deserializer's self-reported consumed length for framing. Alternatively, truncate `buf_bytes` to exactly `payload_len` before calling `read_payload`, and independently verify that the deserializer consumed the entire slice (no trailing unconsumed bytes) before accepting the message.

### Proof of Concept
Add a test in `stackslib/src/net/connection.rs`'s test module that:
1. Constructs a `Preamble` with `payload_len = N`.
2. Builds a payload buffer of `N` bytes where the wire-encoded `StacksMessageType` variant is valid and fully deserializes using fewer than `N` bytes, with the remaining bytes set to a recognizable pattern (e.g., a forged mini `Preamble` for a second attacker message).
3. Feeds `preamble_bytes || payload_bytes || second_message_bytes` into `ConnectionInbox::consume_payload_known_length` (or through the public `next_message` path).
4. Asserts that `next_message_ptr` (or the resulting `self.buf` after consumption) equals `message_ptr + payload_len`, i.e. skips the entire declared payload region; the current code fails this assertion because `next_message_ptr == message_ptr + message_len < message_ptr + payload_len`, leaving the padding bytes to be reparsed as the next preamble.

### Citations

**File:** stackslib/src/net/connection.rs (L818-834)
```rust
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
```
