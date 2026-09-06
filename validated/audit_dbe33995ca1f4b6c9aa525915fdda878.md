### Title
Unauthenticated public-key rebinding of an already-authenticated P2P connection via re-Handshake - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` when the connection has no bound public key yet (`self.connection.get_public_key()` is `None`). Once a connection already has a bound key, the `Some(_)` branch performs no signature verification at all, and its only guard (an address/port equality check) applies solely to outbound connections and is trivially bypassed by setting `addrbytes` to an any-net value. This lets an already-connected, unprivileged peer send a second `Handshake` carrying an arbitrary `node_public_key` that `update_from_handshake_data` will install as the connection's new identity without ever proving ownership of the corresponding private key.

### Finding Description
`validate_handshake` (stackslib/src/net/chat.rs:1052-1127) branches on `self.connection.get_public_key()`:

- `None` branch (chat.rs:1059-1070): calls `message.verify_secp256k1(&handshake_data.node_public_key)`, which is the only proof-of-possession check for the claimed key.
- `Some(_)` branch (chat.rs:1072-1091): performs no signature check whatsoever. It only rejects when `self.stats.outbound` is true **and** `!handshake_data.addrbytes.is_anynet()` **and** the address/port differ from what's already recorded. If the attacker sets `addrbytes` to the any-net value (`0.0.0.0`/`::`), the whole condition is false and even that weak check is skipped — for both inbound and outbound connections.

After this, `validate_handshake` only checks that the claimed key parses (chat.rs:1094-1102), that it's not expired (chat.rs:1104-1111), and that it doesn't equal the local node's own key (chat.rs:1114-1124). None of these prove the sender controls the private key for `handshake_data.node_public_key`.

The caller then invokes `update_from_handshake_data` (chat.rs:1131-1167), which unconditionally does `self.connection.set_public_key(Some(pubk.clone()))` (chat.rs:1164), replacing whatever key was previously bound to this connection with the new, unverified key — even flagging `updated = true` when the key differs (chat.rs:1149-1162), confirming this key-rotation path is reachable and exercised without any signature check.

The exploit: attacker establishes a normal connection (handshakes once with their own real key, populating the `Some` state), then sends a second `Handshake` whose `node_public_key` is any public key of the attacker's choosing (e.g., a victim node's harvested, non-secret `node_public_key` bytes broadcast in that victim's own outbound Handshakes/HandshakeAccepts), with `addrbytes` set to any-net, and a signature that does not need to correspond to the claimed key at all. `validate_handshake` accepts it, and `update_from_handshake_data` rebinds the connection's identity to the unverified key.

### Impact Explanation
This is an authentication-bypass on an established P2P connection: the local peer's record of "who is on the other end of this socket" (`connection`'s bound public key, used for neighbor identity/reputation and any subsequent logic keyed off `get_public_key()`) can be overwritten by an unprivileged remote party without proof of private-key ownership. This matches the Critical "auth bypass" category — the peer's authenticated identity on that connection is forged. The action is repeatable per-connection (each additional Handshake can flip the bound identity again).

### Likelihood Explanation
Preconditions are cheap for an unprivileged remote attacker: connect to the P2P port (no privileged role, no secret needed), complete one normal handshake to reach the `Some(_)` state, then send a crafted second `Handshake` with an arbitrary `node_public_key` and `addrbytes` set to any-net to bypass even the weak outbound address check. No dependency on victim cooperation, no signature forgery is required since none is checked in this branch.

### Recommendation
In the `Some(_)` branch of `validate_handshake`, when `handshake_data.node_public_key` differs from the currently bound key, require `message.verify_secp256k1(&handshake_data.node_public_key)` to succeed before allowing the rebind (i.e., treat key-rotation identically to first-time key establishment: prove ownership of the new key). Do not allow the any-net `addrbytes` value to bypass this signature requirement.

### Proof of Concept
Rust net test outline (in `stackslib/src/net/chat.rs` or `tests/convo.rs`-style test using `convo_send_recv`):
1. Set up `convo_1`/`convo_2` as in existing handshake tests; perform a legitimate handshake so `convo_2.connection.get_public_key()` becomes `Some(pubkey_1)`.
2. Construct a new `StacksMessage::Handshake` with `node_public_key` set to an arbitrary key (e.g. `pubkey_victim`, unrelated to the signer), `addrbytes` set to any-net, and a signature that does not verify against `pubkey_victim` (or leave the preamble signature as produced by the attacker's own unrelated key).
3. Sign/serialize with `convo_1.sign_message` (or manually forge the preamble) and submit via `convo_send_recv(&mut convo_1, &mut convo_2, ...)`.
4. Assert that `convo_2.connection.get_public_key() == Some(pubkey_victim)` even though `message.verify_secp256k1(&pubkey_victim)` was never called — confirming the unauthenticated rebind at chat.rs:1072-1091 and chat.rs:1164. [1](#0-0) [2](#0-1)

### Citations

**File:** stackslib/src/net/chat.rs (L1047-1127)
```rust
    fn validate_handshake(
        &mut self,
        local_peer: &LocalPeer,
        chain_view: &BurnchainView,
        message: &mut StacksMessage,
    ) -> Result<(), net_error> {
        let handshake_data = match message.payload {
            StacksMessageType::Handshake(ref mut data) => data.clone(),
            _ => panic!("Message is not a handshake"),
        };

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
            Some(_) => {
                // for outbound connections, the self-reported address must match socket address if we already have a public key.
                // (not the case for inbound connections, since the peer socket address we see may
                // not be the same as the address the remote peer thinks it has).
                // The only exception to this is if the remote peer does not yet know its own
                // public IP address, in which case, its handshake addrbytes will be the
                // any-network bind address (0.0.0.0 or ::)
                if self.stats.outbound
                    && (!handshake_data.addrbytes.is_anynet()
                        && (self.peer_addrbytes != handshake_data.addrbytes
                            || self.peer_port != handshake_data.port))
                {
                    // wrong peer address
                    debug!(
                        "{:?}: invalid handshake -- wrong addr/port ({:?}:{:?})",
                        &self, &handshake_data.addrbytes, handshake_data.port
                    );
                    return Err(net_error::InvalidHandshake);
                }
            }
        };

        let their_public_key_res = handshake_data.node_public_key.to_public_key();
        match their_public_key_res {
            Ok(_) => {}
            Err(_e) => {
                // bad public key
                debug!("{:?}: invalid handshake -- invalid public key", &self);
                return Err(net_error::InvalidMessage);
            }
        };

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

        Ok(())
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
