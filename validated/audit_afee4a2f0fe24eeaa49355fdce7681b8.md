### Title
Unsolicited `HandshakeAccept`/`StackerDBHandshakeAccept` processed on authenticated conversations without a solicitation check, allowing unauthenticated rekey/state corruption - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationP2P::handle_authenticated_control_message` dispatches `StacksMessageType::HandshakeAccept`/`StackerDBHandshakeAccept` straight to `handle_handshake_accept` with no check that the message was actually solicited (i.e., that this node previously sent a `Handshake` request awaiting this reply), unlike the unauthenticated path which explicitly gates on `self.connection.is_solicited(msg)`. `handle_handshake_accept` then calls `update_from_handshake_data`, which sets `self.connection.set_public_key(...)` using the *embedded* `handshake_accept.handshake.node_public_key` field without any `verify_secp256k1` check that this embedded key was actually possessed/proven by the sender (that check, `validate_handshake`, only exists on the `Handshake` request path, not the `HandshakeAccept` reply path).

### Finding Description
The claimed equality is: "message type processed by the state machine == message type valid for the current session/request state." For the authenticated branch this equality is broken: [1](#0-0) 

Here, `StacksMessageType::HandshakeAccept`/`StackerDBHandshakeAccept` are unconditionally routed to `self.handle_handshake_accept(...)`, with no `self.connection.is_solicited(msg)` gate — contrast this with the *unauthenticated* branch which explicitly requires `solicited` before calling the same handler: [2](#0-1) 

`handle_handshake_accept` then calls `update_from_handshake_data(preamble, &handshake_accept.handshake)`: [3](#0-2) 

`update_from_handshake_data` sets the connection's tracked public key directly from the attacker-supplied `node_public_key` field embedded in the payload, without any signature/possession proof over that specific field: [4](#0-3) 

This is in sharp contrast to the `Handshake` (request) path, where `validate_handshake` calls `message.verify_secp256k1(&handshake_data.node_public_key)` to prove the sender possesses the private key for the embedded public key when one is not already known: [5](#0-4) 

No equivalent check exists for the `HandshakeAccept` payload's nested `handshake_accept.handshake.node_public_key`.

Attacker flow: an unprivileged remote party (1) completes a normal handshake with the node (this is freely available to anyone who can connect — no privilege required), which makes `self.connection.has_public_key()` true and routes all further messages through `handle_authenticated_control_message`; (2) at any later time, sends an unsolicited `StacksMessageType::HandshakeAccept` (or `StackerDBHandshakeAccept`) message, correctly signed with their own real (already-known) private key so it passes the *outer* message signature check performed by `ConnectionP2P`, but with an arbitrary `HandshakeAcceptData.handshake.node_public_key` field of the attacker's choosing (any syntactically-valid secp256k1 public key, not necessarily one the attacker controls). Because there is no solicitation check and no `verify_secp256k1` proof-of-possession over this embedded field, the node accepts it and calls `self.connection.set_public_key(Some(forged_pubk))`, overwriting the trusted public key binding for this conversation.

### Impact Explanation
This corrupts the `ConversationP2P`'s notion of "which peer identity this socket/connection belongs to" using unauthenticated, unproven data. Effects include:
- Rekeying the conversation to an attacker-chosen key not proven to be possessed by the peer, silently changing `peer_services`, `peer_expire_block_height`, `handshake_addrbytes`/`handshake_port`, and `data_url` fields tracked for that neighbor's identity — state corruption reachable with a single crafted, unsolicited message, repeatable at will.
- Because subsequent inbound messages on this connection must be signed against the new (attacker-chosen) key for `ConnectionP2P`'s signature check to succeed, if the attacker sets the key to one they don't control, the conversation is effectively bricked (denial of service against that specific peer session); if they set it to a *real* known third party's public key, this creates confusion between the sending socket and an unrelated real peer's identity within this node's local `ConversationP2P`/stats bookkeeping.
This matches "unauthenticated ... state corruption" — degrading peer-identity tracking integrity using a single unsolicited reply message from an unprivileged, already-connected peer.

### Likelihood Explanation
Preconditions: the attacker only needs to be able to complete a normal, unprivileged handshake with the target node (any remote party can do this — no secret, no admin role, no local access). Once `has_public_key()` is true for the conversation, the attacker can send the crafted `HandshakeAccept`/`StackerDBHandshakeAccept` message at any time, any number of times, entirely under their control and free of rate limiting beyond normal message processing. This is a low-cost, remotely reachable, fully repeatable action.

### Recommendation
In `handle_authenticated_control_message` (stackslib/src/net/chat.rs), gate the `HandshakeAccept`/`StackerDBHandshakeAccept` arms on `self.connection.is_solicited(msg)` exactly as is already done in `handle_unauthenticated_control_message`, discarding/ignoring unsolicited replies without invoking `handle_handshake_accept`. Additionally, `update_from_handshake_data`/`handle_handshake_accept` should verify (via `verify_secp256k1` or equivalent) that the embedded `handshake_accept.handshake.node_public_key` corresponds to the key that actually signed the enclosing `StacksMessage`, rather than trusting the payload's self-reported key unconditionally.

### Proof of Concept
Rust net test plan (in `stackslib/src/net/chat.rs` test module, alongside existing `ConversationP2P` handshake tests):
1. Set up two `ConversationP2P` instances (`convo_1`, `convo_2`) as in existing tests (e.g. `convo_handshake_accept`), complete a full legitimate handshake so `convo_2.connection.has_public_key()` is `true` for the node-under-test's conversation representing the attacker.
2. Craft a `StacksMessage` with payload `StacksMessageType::HandshakeAccept(HandshakeAcceptData { handshake: HandshakeData { node_public_key: <arbitrary/unrelated StacksPublicKeyBuffer>, .. }, .. })`, sign it with the attacker's real (already-known) private key, and set an arbitrary/unmatched `seq`/`request_id` so `self.connection.is_solicited(&msg)` would return `false` for this message (i.e., it is not a reply to any actual outstanding request).
3. Feed this message directly into the target conversation's inbox and call `chat()` (or directly call `handle_authenticated_control_message`).
4. Assert that `self.connection.get_public_key()` after `chat()` equals the *original* pre-handshake public key (expected/fixed behavior) — currently it will instead equal the attacker-forged `node_public_key`, demonstrating the state-corruption; also assert no `is_solicited` check occurred, showing the unsolicited/unexpected reply was still fully processed.

### Citations

**File:** stackslib/src/net/chat.rs (L1058-1071)
```rust
        match self.connection.get_public_key() {
            None => {
                // if we don't yet have a public key for this node, verify the message.
                // if it's improperly signed, it's probably a poorly-timed re-key request (but either way the message should be rejected)
                message
                    .verify_secp256k1(&handshake_data.node_public_key)
                    .map_err(|_e| {
                        debug!(
                            "{:?}: invalid handshake: not signed with given public key",
                            &self
                        );
                        net_error::InvalidMessage
                    })?;
            }
```

**File:** stackslib/src/net/chat.rs (L1131-1167)
```rust
    pub fn update_from_handshake_data(
        &mut self,
        preamble: &Preamble,
        handshake_data: &HandshakeData,
    ) -> Result<bool, net_error> {
        let pubk = handshake_data
            .node_public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;

        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();

        let mut updated = false;
        let cur_pubk_opt = self.connection.get_public_key();
        if let Some(cur_pubk) = cur_pubk_opt {
            if pubk != cur_pubk {
                debug!(
                    "{:?}: Upgrade key {:?} to {:?} expires {:?}",
                    &self,
                    &to_hex(&cur_pubk.to_bytes_compressed()),
                    &to_hex(&pubk.to_bytes_compressed()),
                    self.peer_expire_block_height
                );
                updated = true;
            }
        }

        self.connection.set_public_key(Some(pubk.clone()));

        Ok(updated)
    }
```

**File:** stackslib/src/net/chat.rs (L1338-1345)
```rust
    fn handle_handshake_accept(
        &mut self,
        burnchain_view: &BurnchainView,
        preamble: &Preamble,
        handshake_accept: &HandshakeAcceptData,
        stackerdb_accept: Option<&StackerDBHandshakeData>,
    ) -> Result<(), net_error> {
        self.update_from_handshake_data(preamble, &handshake_accept.handshake)?;
```

**File:** stackslib/src/net/chat.rs (L2548-2562)
```rust
            StacksMessageType::HandshakeAccept(ref data) => {
                debug!("{self:?}: Got HandshakeAccept");
                self.handle_handshake_accept(network.get_chain_view(), &msg.preamble, data, None)
                    .map(|_| None)
            }
            StacksMessageType::StackerDBHandshakeAccept(ref data, ref db_data) => {
                debug!("{self:?}: Got StackerDBHandshakeAccept");
                self.handle_handshake_accept(
                    network.get_chain_view(),
                    &msg.preamble,
                    data,
                    Some(db_data),
                )
                .map(|_| None)
            }
```

**File:** stackslib/src/net/chat.rs (L2625-2642)
```rust
            StacksMessageType::HandshakeAccept(ref data) => {
                if solicited {
                    debug!("{:?}: Got unauthenticated HandshakeAccept", &self);
                    self.handle_handshake_accept(
                        network.get_chain_view(),
                        &msg.preamble,
                        data,
                        None,
                    )
                    .map(|_| None)
                } else {
                    debug!("{:?}: Unsolicited unauthenticated HandshakeAccept", &self);

                    // don't update stats or state, and don't pass back
                    consume = true;
                    Ok(None)
                }
            }
```
