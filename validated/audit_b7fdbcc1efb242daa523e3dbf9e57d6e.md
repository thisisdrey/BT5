### Title
Handshake re-key path skips signature verification of the newly claimed public key, allowing an outbound peer to inject an unverified `public_key` into PeerDB - [File: stackslib/src/net/chat.rs -> ConversationP2P::validate_handshake / update_from_handshake_data / handle_handshake]

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` when `self.connection.get_public_key()` is `None` (first handshake on the convo). Once a convo already has a known key (any subsequent handshake, i.e. a "re-key"), the function performs no signature check against the newly claimed `node_public_key` at all - it only optionally checks the self-reported address/port for outbound convos, then unconditionally accepts. `update_from_handshake_data` then overwrites `self.connection`'s public key with this unverified value, and for outbound convos `handle_handshake` writes a `Neighbor` record with that unverified `public_key` into PeerDB.

### Finding Description
The claimed equality is real: for a convo where `self.connection.get_public_key()` is already `Some(A)`, `validate_handshake` enters the `Some(_)` branch: [1](#0-0) 
This branch never calls `message.verify_secp256k1(&handshake_data.node_public_key)` - that call only happens in the `None` branch: [2](#0-1) 
The remaining checks in `validate_handshake` (public-key format, expiry, "not from myself") do not verify that `handshake_data.node_public_key` corresponds to whoever actually signed `message.preamble.signature`: [3](#0-2) 

`handle_handshake` then calls `update_from_handshake_data`, which derives `pubk` purely from the wire-controlled `handshake_data.node_public_key` field and unconditionally installs it via `self.connection.set_public_key(Some(pubk.clone()))`, returning `updated = true` whenever `pubk != cur_pubk`: [4](#0-3) 

For outbound convos, `handle_handshake` treats `updated == true` as a legitimate re-key event and persists it to PeerDB: [5](#0-4) 

Exploit flow: an attacker runs a peer that the victim node dials out to (a normal, unprivileged position for any node in the peer's neighbor/bootstrap list). The attacker completes a legitimate first handshake using their own real key `A` (satisfying the `None` branch's `verify_secp256k1` check honestly). On a later Handshake message over the same convo, the attacker sets `handshake_data.node_public_key = B` (any other node's real public key, or an arbitrary valid key) while still signing the message preamble/payload with their own private key `A` (which they possess). Because the convo already has `Some(A)`, `validate_handshake`'s `Some(_)` branch is taken, the address/port check (if applicable) is satisfiable trivially since it concerns the socket address of the already-established outbound convo, and no signature check against `B` ever executes. `update_from_handshake_data` overwrites the convo's known key with `B` and reports `updated = true`, and `handle_handshake` writes a `Neighbor` record with `public_key = B` into the local PeerDB, associated with this convo's address, without any proof the responder ever held `B`'s private key.

### Impact Explanation
The local node's PeerDB is updated (unauthenticated write to persistent peer state) to associate a network address with a public key that was never proven via signature - this is a forged identity binding that the local node may subsequently gossip to other peers via neighbor-walk protocol, propagating the false address-to-key mapping network-wide. This matches the Critical category "unauthenticated/unauthorized write to state ... network-wide propagation of forged data." The write only occurs when `updated && self.stats.outbound` is true, so the vulnerable path requires the victim node to have dialed out to the attacker-controlled address (a normal occurrence for any P2P node performing neighbor walks/handshakes).

### Likelihood Explanation
Preconditions: an attacker-controlled peer must be reachable as an outbound target for the victim (trivial - any node advertising itself as a neighbor, or a bootstrap peer, gets dialed by others) and must complete one legitimate initial handshake using its own real key. After that, sending a second Handshake message with an arbitrary `node_public_key` field is a single crafted message, fully repeatable per convo, requiring no special privileges, secrets, or additional capabilities beyond normal P2P participation.

### Recommendation
In `validate_handshake`'s `Some(_)` branch, when `handshake_data.node_public_key` differs from `self.connection.get_public_key()`, require `message.verify_secp256k1(&handshake_data.node_public_key)` to succeed (proving possession of the new key's private key) before allowing the re-key, in addition to (not instead of) the existing address/port check.

### Proof of Concept
Rust test in `stackslib/src/net/chat.rs` test module:
1. Construct a `ConversationP2P` for an outbound convo and drive an initial handshake with private key `A`, asserting `handle_handshake` succeeds and `self.connection.get_public_key() == Some(pubkey_A)`.
2. Construct a second `StacksMessage::Handshake` with `handshake_data.node_public_key = pubkey_B` (a different keypair) but sign the message (`msg.sign(seq, &privkey_A)`) using `privkey_A`.
3. Feed this message into `handle_handshake` on the same convo.
4. Assert expectation: `handle_handshake` should reject (`Err(net_error::InvalidHandshake)` or similar) since the signature does not correspond to `pubkey_B`; assert that no PeerDB `Neighbor` row with `public_key = pubkey_B` is created/updated for this address.
5. Current behavior (bug): `handle_handshake` returns `Ok(...)`, `self.connection.get_public_key() == Some(pubkey_B)`, and (for outbound convos) `Neighbor::load_and_update`/`save_update` writes `public_key = pubkey_B` to PeerDB - demonstrating the unauthenticated write.

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

**File:** stackslib/src/net/chat.rs (L1072-1092)
```rust
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

**File:** stackslib/src/net/chat.rs (L1094-1126)
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
