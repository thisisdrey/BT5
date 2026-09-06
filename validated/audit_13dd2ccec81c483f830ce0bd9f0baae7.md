### Title
Unauthenticated in-place public-key rebind on established inbound P2P sessions - (File: `stackslib/src/net/chat.rs`)

### Summary
`ConversationP2P::validate_handshake` only checks the handshake signature against the *new* claimed key when no key is yet known for the connection (`self.connection.get_public_key() == None`); once a key is already associated with the connection, the `Some(_)` branch performs no signature check at all, and the address-continuity check it does perform is gated on `self.stats.outbound`. On an inbound connection this means a second `Handshake` announcing a brand-new key `K2` is accepted and immediately trusted by `update_from_handshake_data`/`connection.set_public_key()` with zero cryptographic proof that the sender controls `K1` (the previously proven key) or `K2`, and with no re-verification that `K2` is bound to the address that `K1` proved.

### Finding Description
`validate_handshake` branches on `self.connection.get_public_key()`: [1](#0-0) 
- `None` branch: verifies the message signature against `handshake_data.node_public_key` (i.e. it checks the *new* key signed the message) — this is the only signature check in the whole function.
- `Some(_)` branch: for **outbound** connections only, checks that `handshake_data.addrbytes`/`port` matches the socket address already proven for this peer. For **inbound** connections, this branch does nothing — no signature check against the old key, no signature check against the new key, no address check at all.

After `validate_handshake` returns `Ok`, `handle_handshake` calls `update_from_handshake_data`, which unconditionally overwrites the trusted key: [2](#0-1) 

The only remaining gates are "not stale" (`expire_block_height`) and "not our own key" — neither of which requires proving continuity with the previously-authenticated key `K1` or address `A`. The PeerDB persistence path that re-validates/records the re-key is also skipped for inbound connections because it is gated on `self.stats.outbound`: [3](#0-2) 

Reachability: `handle_handshake` is invoked from both the authenticated and unauthenticated dispatchers for any `Handshake` payload: [4](#0-3) [5](#0-4) 
A handshake that doesn't verify against the currently-known key `K1` is routed to the *unauthenticated* handler, which still processes `Handshake` messages regardless of authentication status (this is by design, since a handshake is how a connection becomes authenticated). Combined with the missing signature check in the `Some(_)` branch of `validate_handshake`, this means a `Handshake` message that is not cryptographically tied to `K1` in any way (garbage/self-signed by an arbitrary `K2`) still flows through `handle_handshake` → `update_from_handshake_data` and overwrites `connection.public_key` from `K1` to `K2` on an inbound session, with the address/port claims never checked.

### Impact Explanation
This lets any remote party who can complete or observe an inbound TCP session silently rebind that session's trusted identity key without proving possession of the old key or any continuity of the claimed address. Since the peer's trusted key gates subsequent authenticated control-plane and data-plane message handling (`handle_authenticated_control_message`), this is an unauthenticated identity takeover of an established inbound session — matching the Critical category ("unauthenticated/unauthorized write to state", here in-memory session trust state that downstream logic treats as authenticated). It is repeatable per inbound connection and requires only sending a second `Handshake` message on an already-open socket.

### Likelihood Explanation
Precondition is simply having (or being) an inbound P2P connection that has already handshaked once (trivial and expected in normal operation — any remote node handshakes inbound). No secrets, no privileged role, and no address-continuity proof are required to trigger the rekey path for inbound sessions; attacker cost is one additional `Handshake` message. The P2P port is remotely reachable by design.

### Recommendation
Remove the `self.stats.outbound` gate on the address-continuity check in `validate_handshake` so inbound re-key requests are also validated against the previously-proven address (or explicitly require the anynet exception only), and additionally require that a re-key handshake either (a) be signed by the currently-known key `K1` proving continuity, or (b) be signed by the new key `K2` and independently re-verified/persisted the same way the outbound path does at `chat.rs:1275-1295`, regardless of connection direction.

### Proof of Concept
Rust test plan in `stackslib/src/net/chat.rs` test module:
1. Build `convo_1` (outbound) and `convo_2` (inbound, `self.stats.outbound == false`), complete a full handshake so `convo_2.connection.get_public_key() == Some(K1)`.
2. Craft a second `StacksMessageType::Handshake` with `node_public_key = K2` (freshly generated, unrelated keypair) and `addrbytes`/`port` left as `A` or set to anynet; sign it with `K2` (or leave signature not matching `K1`).
3. Feed this message into `convo_2.chat(...)` (or directly call `convo_2.handle_handshake(&mut network, &mut msg, false, false)`), simulating dispatch through `handle_unauthenticated_control_message`.
4. Assert `convo_2.connection.get_public_key().unwrap() == K2`, confirming the key flipped with no address/signature continuity check, proving the described equality violation.

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

**File:** stackslib/src/net/chat.rs (L1149-1166)
```rust
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

**File:** stackslib/src/net/chat.rs (L2540-2546)
```rust
            StacksMessageType::Handshake(_) => {
                monitoring::increment_msg_counter("p2p_authenticated_handshake".to_string());

                debug!("{self:?}: Got Handshake");
                let (handshake_opt, handled) = self.handle_handshake(network, msg, true, ibd)?;
                consume = handled;
                Ok(handshake_opt)
```

**File:** stackslib/src/net/chat.rs (L2617-2624)
```rust
        let reply_opt = match msg.payload {
            StacksMessageType::Handshake(_) => {
                monitoring::increment_msg_counter("p2p_unauthenticated_handshake".to_string());
                debug!("{:?}: Got unauthenticated Handshake", &self);
                let (reply_opt, handled) = self.handle_handshake(network, msg, false, ibd)?;
                consume = handled;
                Ok(reply_opt)
            }
```
