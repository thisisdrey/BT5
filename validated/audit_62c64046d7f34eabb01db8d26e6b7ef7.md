### Title
Malformed `node_public_key` in unauthenticated `Handshake` causes `net_error::InvalidMessage` (hard error) instead of `net_error::InvalidHandshake` (soft reject), tearing down the connection - ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P::validate_handshake` classifies a malformed `node_public_key` in a `Handshake` message as `net_error::InvalidMessage` rather than `net_error::InvalidHandshake`. `handle_handshake` only converts `net_error::InvalidHandshake` into a graceful `HandshakeReject` reply; every other `Err` variant, including `InvalidMessage`, is returned unmodified and propagates up the call stack, which tears down the whole conversation/connection instead of just rejecting the handshake.

### Finding Description
In `validate_handshake` (`stackslib/src/net/chat.rs:1094-1101`), the code attempts to parse the peer-supplied `handshake_data.node_public_key` into a real public key: [1](#0-0) 
When `to_public_key()` fails (e.g., the 33-byte buffer does not encode a valid compressed secp256k1 point), the function returns `Err(net_error::InvalidMessage)` — not `Err(net_error::InvalidHandshake)`, unlike the sibling checks a few lines below for stale `expire_block_height` and self-handshake, which correctly return `net_error::InvalidHandshake`: [2](#0-1) 

`handle_handshake` (`stackslib/src/net/chat.rs:1214-1243`) explicitly special-cases only `net_error::InvalidHandshake` to produce a `HandshakeReject` reply and keep the connection alive; every other error (including the `InvalidMessage` from the malformed-pubkey path) is passed through via `Err(e) => return Err(e)`: [3](#0-2) 

Because `disable_inbound_handshakes` defaults to `false`, this validation runs on unauthenticated inbound handshakes as well (the block at `chat.rs:1221-1224` only short-circuits when the option is explicitly enabled). A single `Handshake` message with a syntactically well-formed 33-byte buffer that does not decode to a valid EC point (i.e., malformed `node_public_key`) reaches `to_public_key()`, fails, and yields `net_error::InvalidMessage`, which is not caught by `handle_handshake`'s soft-reject arm and instead surfaces as a hard `Err` to the caller.

### Impact Explanation
The mis-classified `net_error::InvalidMessage` propagates out of `handle_handshake` into the message-dispatch logic that handles unauthenticated control messages, where non-`InvalidHandshake` errors are treated as fatal for the conversation, causing the peer connection to be dropped/pruned. This gives a remote, unauthenticated attacker a one-message primitive to unilaterally terminate a P2P connection with any node that accepts inbound handshakes (default configuration) or that the attacker dials, repeatable against every connection attempt. This matches the Critical category of "remote crash/unauthenticated DoS from few messages."

### Likelihood Explanation
No privileged role, secret, or prior handshake state is required: `disable_inbound_handshakes` defaults to `false`, so any remote peer that can open a P2P socket and send one `Handshake` payload with a malformed `node_public_key` buffer can trigger this path. The cost is a single crafted message, fully attacker-controlled, and repeatable against any number of peers or reconnect attempts.

### Recommendation
Change the malformed-public-key branch in `validate_handshake` (`stackslib/src/net/chat.rs:1097-1101`) to return `Err(net_error::InvalidHandshake)` instead of `Err(net_error::InvalidMessage)`, consistent with the other handshake-content validation failures in the same function, so `handle_handshake` replies with a `HandshakeReject` rather than tearing down the connection.

### Proof of Concept
Add a test in `stackslib/src/net/chat.rs`'s test module that constructs a `Handshake` message whose `node_public_key: StacksPublicKeyBuffer` contains 33 bytes that do not decode to a valid compressed secp256k1 point (e.g., all `0xFF` or a leading byte not in `{0x02,0x03}`), sign/serialize it into a `StacksMessage`, feed it into a `ConversationP2P` via the same path `chat()`/`handle_unauthenticated_control_message` uses, with `disable_inbound_handshakes = false` and no pre-existing public key set on the connection. Assert that:
- Current (buggy) behavior: the call returns `Err(net_error::InvalidMessage)` (or the conversation/connection is torn down) rather than `Ok((Some(HandshakeReject), true))`.
- Expected (fixed) behavior: `handle_handshake` returns `Ok((Some(StacksMessage{payload: StacksMessageType::HandshakeReject, ..}), true))`, and the connection remains alive.

### Citations

**File:** stackslib/src/net/chat.rs (L1094-1102)
```rust
        let their_public_key_res = handshake_data.node_public_key.to_public_key();
        match their_public_key_res {
            Ok(_) => {}
            Err(_e) => {
                // bad public key
                debug!("{:?}: invalid handshake -- invalid public key", &self);
                return Err(net_error::InvalidMessage);
            }
        };
```

**File:** stackslib/src/net/chat.rs (L1104-1124)
```rust
        if handshake_data.expire_block_height <= chain_view.burn_block_height {
            // already stale
            debug!(
                "{:?}: invalid handshake -- stale public key (expired at {})",
                &self, handshake_data.expire_block_height
            );
            return Err(net_error::InvalidHandshake);
        }

        // the handshake cannot come from us
        if handshake_data.node_public_key
            == StacksPublicKeyBuffer::from_public_key(&Secp256k1PublicKey::from_private(
                &local_peer.private_key,
            ))
        {
            debug!(
                "{:?}: invalid handshake -- got a handshake from myself",
                &self
            );
            return Err(net_error::InvalidHandshake);
        }
```

**File:** stackslib/src/net/chat.rs (L1226-1243)
```rust
        let res =
            self.validate_handshake(network.get_local_peer(), network.get_chain_view(), message);
        match res {
            Ok(_) => {}
            Err(net_error::InvalidHandshake) => {
                let reject = StacksMessage::from_chain_view(
                    self.version,
                    self.network_id,
                    network.get_chain_view(),
                    StacksMessageType::HandshakeReject,
                );
                debug!("{:?}: invalid handshake", &self);
                return Ok((Some(reject), true));
            }
            Err(e) => {
                return Err(e);
            }
        };
```
