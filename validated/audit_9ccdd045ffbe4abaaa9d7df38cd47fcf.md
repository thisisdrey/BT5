### Title
Authentication bypass in handshake re-keying: `ConversationP2P::validate_handshake`'s `Some(_)` branch omits signature verification, allowing unauthenticated identity hijack - ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` in the `None` arm (first-ever handshake); the `Some(_)` arm — used once a peer already has an authenticated public key — performs only an address/port sanity check and never verifies the message signature against either the current key or the newly claimed key. `update_from_handshake_data` then unconditionally overwrites `self.connection`'s public key with whatever `node_public_key` the attacker put in the payload, and for outbound connections this bogus key is persisted to the `PeerDB` via `Neighbor::load_and_update`/`save_update`.

### Finding Description [1](#0-0) 
In the `None` branch, `message.verify_secp256k1(&handshake_data.node_public_key)` proves the sender possesses the private key for the claimed identity before it is trusted. In the `Some(_)` branch (already-authenticated peer), the only check performed is an outbound-address match — no call to `verify_secp256k1` against either the old (`cur_pubk`) or new (`handshake_data.node_public_key`) key exists anywhere in that branch.

This directly contradicts the function's own documented invariant at [2](#0-1) 
which states a handshake "will only be accepted if we do not yet know the public key of this remote peer, or if it is signed by the current public key" — the code never enforces the "signed by the current public key" half of that contract.

`update_from_handshake_data` then blindly trusts the unverified `handshake_data.node_public_key`: [3](#0-2) 
If `pubk != cur_pubk`, it sets `updated = true` and calls `self.connection.set_public_key(Some(pubk.clone()))` unconditionally — no signature-based proof of possession of `pubk` is required.

Back in `handle_handshake`, an `updated == true` result on an outbound connection triggers a **persistent write** to local state: [4](#0-3) 
`Neighbor::load_and_update` + `neighbor.save_update(&tx, None)` writes the attacker-chosen public key into the `PeerDB` neighbor record, with no proof the attacker (or anyone) holds the corresponding private key.

Exploit flow: attacker completes a legitimate handshake with key `pubkA` (session's `get_public_key()` becomes `Some(pubkA)`). Attacker then sends a second `Handshake` message whose payload declares `node_public_key = pubkB` (an arbitrary key the attacker may not even control the private key for, or a key belonging to a victim node) and signs the message with `pubkA`'s key (or, since the branch never checks the signature at all, potentially with any signature that merely deserializes). `validate_handshake` accepts it (only address check), `update_from_handshake_data` swaps the connection's authenticated identity to `pubkB`, and if the connection is outbound, this bogus identity is persisted into the local `PeerDB`.

### Impact Explanation
This is an unauthenticated write to local node state: an already-connected but otherwise unprivileged peer can rewrite the locally-stored authenticated public key for a `PeerDB` neighbor entry to an arbitrary value without proving possession of the corresponding private key. This corrupts the node's neighbor/authentication state (`PeerDB`), can be used to impersonate or deny a legitimate peer's identity in the local node's records, and undermines the entire handshake-based peer-authentication model for subsequent authenticated-message checks tied to `self.connection.get_public_key()`. This matches the Critical category "unauthenticated/unauthorized write to state."

### Likelihood Explanation
Precondition is only that an initial legitimate handshake has already occurred (trivial — any remote party can initiate a P2P handshake with a node, no special privilege or secret required). The attacker then needs to send one additional `Handshake` message; the address check in the `Some(_)` branch is easily satisfied for inbound connections (which skip the address check entirely) or matched trivially for outbound connections the attacker controls. No admin role, RPC secret, or other party's key is needed — fully remotely reachable and repeatable per connection.

### Recommendation
In the `Some(_)` branch of `validate_handshake`, require `message.verify_secp256k1(&cur_pubk)` (to allow continuation under the same identity) or, if key rotation is intentionally supported, require `message.verify_secp256k1(&handshake_data.node_public_key)` to prove possession of the new key before accepting it, matching the behavior already implemented in the `None` branch and the documented contract on `handle_handshake`.

### Proof of Concept
Rust test in `stackslib/src/net/chat.rs` test module:
1. Establish `convo_a`/`convo_b` and complete a normal handshake so `convo_b.connection.get_public_key() == Some(pubkA)`.
2. Craft a second `Handshake` `StacksMessage` with `handshake_data.node_public_key = pubkB` (freshly generated, unrelated keypair), sign it with `pubkA`'s key (or an unrelated/garbage signature), and feed it into `convo_b.validate_handshake(...)` followed by `convo_b.update_from_handshake_data(...)`.
3. Assert `validate_handshake` returns `Ok(())` and `update_from_handshake_data` returns `Ok(true)`, with `convo_b.connection.get_public_key() == Some(pubkB)` — demonstrating the connection's authenticated identity was hijacked without any signature check proving possession of `pubkB`'s private key.

### Citations

**File:** stackslib/src/net/chat.rs (L1058-1092)
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
```

**File:** stackslib/src/net/chat.rs (L1136-1164)
```rust
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
```

**File:** stackslib/src/net/chat.rs (L1208-1213)
```rust
    /// Handle an inbound handshake request, and generate either a HandshakeAccept or a HandshakeReject
    /// payload to send back.
    /// A handshake will only be accepted if we do not yet know the public key of this remote peer,
    /// or if it is signed by the current public key.
    /// Returns a reply (either an accept or reject) if appropriate
    /// Panics if this message is not a handshake (caller should check)
```

**File:** stackslib/src/net/chat.rs (L1275-1295)
```rust
        if updated && self.stats.outbound {
            // save the new key
            let tx = network.peerdb_tx_begin().map_err(net_error::DBError)?;
            let (mut neighbor, _) = Neighbor::load_and_update(
                &tx,
                message.preamble.peer_version,
                message.preamble.network_id,
                &handshake_data,
            )?;
            neighbor.save_update(&tx, None)?;
            tx.commit()
                .map_err(|e| net_error::DBError(db_error::SqliteError(e)))?;

            debug!(
                "{:?}: Re-key {:?} to {:?} expires {}",
                network.get_local_peer(),
                &neighbor.addr,
                &to_hex(&neighbor.public_key.to_bytes_compressed()),
                neighbor.expire_block
            );
        }
```
