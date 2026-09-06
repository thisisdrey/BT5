### Title
Replayed/stale signed Handshake message rolls back peer key & PeerDB state because `validate_handshake` skips signature re-verification once a public key is already known - (File: `stackslib/src/net/chat.rs`)

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1()` when `self.connection.get_public_key()` is `None`. Once a peer has already handshaked once (key known), any subsequent `Handshake` message — including a stale, previously-accepted one that an attacker replays — is accepted by `validate_handshake` without any cryptographic freshness or ordering check, and `update_from_handshake_data`/`PeerDB::update_peer` will happily roll the conversation's/PeerDB's key and `expire_block_height` back to the older values.

### Finding Description
`validate_handshake` (stackslib/src/net/chat.rs:1047-1127) branches on whether a public key is already known:
- `None` branch: verifies the message is signed by the embedded `node_public_key` [1](#0-0) .
- `Some(_)` branch: performs **no signature check at all**. For outbound connections it only checks that `handshake_data.addrbytes/port` match the socket's known address; for inbound connections it performs no check whatsoever [2](#0-1) .

Because `Preamble.seq`/`ReceiverNotify::expected_seq` are only used to route replies to a waiting handle (not enforced as a monotonic anti-replay counter), and because `validate_handshake`'s `Some(_)` branch never re-verifies the handshake against the currently-known key, a captured/old `Handshake` byte blob (originally valid, signed by the peer's *previous* key `K1`) can be replayed after the peer has legitimately re-keyed to `K2`. `handle_handshake` (stackslib/src/net/chat.rs:1214-1296) calls `update_from_handshake_data`, which compares the embedded key against `self.connection.get_public_key()` (now `K2`) and finds them different, so it sets `updated = true` and calls `self.connection.set_public_key(Some(K1))` — silently downgrading the in-memory conversation state to the old key/expire height [3](#0-2) . If the connection is outbound, `handle_handshake` then persists this stale state via `Neighbor::load_and_update` + `neighbor.save_update` → `PeerDB::update_peer`, overwriting the legitimately re-keyed record on disk with the old `public_key`/`expire_block_height` and resetting `last_contact_time` [4](#0-3) [5](#0-4) [6](#0-5) .

The routing to authenticated vs. unauthenticated handling (`handle_authenticated_control_message` / `handle_unauthenticated_control_message`, stackslib/src/net/chat.rs:2528-2600, 2602-2707) does not fix this: `Handshake` is explicitly accepted through the unauthenticated path too [7](#0-6) , and regardless of which path dispatches it, `handle_handshake` calls the same `validate_handshake`/`update_from_handshake_data` logic that lacks freshness/replay binding.

### Impact Explanation
An attacker who has legitimately handshaked once (i.e., holds/retains the raw bytes of their own prior, validly-signed `Handshake` message) can later replay that exact frame to roll back the victim's stored view of the attacker's own key/services/expiry — both in the live `ConversationP2P` state and, for outbound connections, in `PeerDB` via `PeerDB::update_peer`. This is an unauthenticated/unauthorized state overwrite: a previously-authenticated key can be "resurrected" and newer, legitimately re-keyed state can be clobbered without a fresh signature, resetting `last_contact_time` and stale `expire_block_height`/`services`. This matches the Critical category of "unauthenticated/unauthorized write to state," though the affected state is scoped to the attacker's own PeerDB entry (self-referential rollback) rather than another party's identity, since `validate_handshake`'s address check (outbound case) still ties the handshake to the correct peer address/port.

### Likelihood Explanation
Preconditions are minimal and fully within reach of an unprivileged remote peer: complete one handshake, retain the raw wire bytes of that handshake, then replay them verbatim at any later time (even after re-keying). No secret, admin role, or privileged access is required — this is exploitable by any peer that can complete a normal handshake and later reconnect (or even keep the connection open) and resend the old bytes. The attack is repeatable indefinitely since nothing in `validate_handshake`'s `Some(_)` branch or the seq/`expected_seq` mechanism binds the message to freshness or a specific point in time.

### Recommendation
In `validate_handshake`'s `Some(_)` branch, always re-verify the handshake signature (either against the currently known public key when unchanged, or against the embedded key when a key upgrade is claimed) rather than skipping cryptographic verification entirely. Additionally, reject handshakes whose `expire_block_height` or an explicit monotonic nonce/timestamp is not strictly greater than the value already recorded for that peer, to prevent stale-but-validly-signed handshakes from rolling back previously-accepted, fresher state.

### Proof of Concept
Extend the existing rekey test in `stackslib/src/net/chat.rs` (the pattern around lines 4950-5009 that already regenerates `local_peer_1`'s key and performs `convo_handshake_rekey`):
1. Perform an initial handshake between `convo_1` and `convo_2`; capture the raw serialized bytes of `handshake_1` (signed with `K1`).
2. Regenerate `local_peer_1`'s key to `K2` (`PeerDB::set_local_private_key`) and perform a legitimate re-key handshake, confirming `PeerDB::get_peer` for peer 1 on `convo_2`'s side reflects `K2` and the new `expire_block_height`.
3. Replay the captured `handshake_1` bytes (signed with `K1`) into `convo_2` via `consume_payload_known_length`/`chat()`.
4. Assert that `convo_2.connection.get_public_key()` (and, for the outbound side, `PeerDB::get_peer`) has been rolled back to `K1`/the old `expire_block_height`, and that `last_contact_time` was reset — demonstrating the stale-state overwrite despite no fresh signature being produced.

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

**File:** stackslib/src/net/chat.rs (L1129-1167)
```rust
    /// Update connection state from handshake data.
    /// Returns true if we learned a new public key; false if not
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

**File:** stackslib/src/net/chat.rs (L1275-1296)
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

**File:** stackslib/src/net/neighbors/neighbor.rs (L50-61)
```rust
    pub fn save_update(
        &mut self,
        tx: &DBTx<'_>,
        stacker_dbs: Option<&[QualifiedContractIdentifier]>,
    ) -> Result<(), net_error> {
        self.last_contact_time = get_epoch_time_secs();
        PeerDB::update_peer(tx, self).map_err(net_error::DBError)?;
        if let Some(stacker_dbs) = stacker_dbs {
            PeerDB::update_peer_stacker_dbs(tx, self, stacker_dbs).map_err(net_error::DBError)?;
        }
        Ok(())
    }
```

**File:** stackslib/src/net/db.rs (L1285-1333)
```rust
    /// Update an existing peer's entries.  Does nothing if the peer is not present.
    pub fn update_peer(tx: &Transaction, neighbor: &Neighbor) -> Result<(), db_error> {
        let old_peer_opt = PeerDB::get_peer(
            tx,
            neighbor.addr.network_id,
            &neighbor.addr.addrbytes,
            neighbor.addr.port,
        )?;

        let args = params![
            neighbor.addr.peer_version,
            to_hex(&neighbor.public_key.to_bytes_compressed()),
            u64_to_sql(neighbor.expire_block)?,
            u64_to_sql(neighbor.last_contact_time)?,
            neighbor.asn,
            neighbor.org,
            neighbor.allowed,
            neighbor.denied,
            neighbor.in_degree,
            neighbor.out_degree,
            !neighbor.addr.addrbytes.is_in_private_range(),
            neighbor.addr.network_id,
            to_bin(neighbor.addr.addrbytes.as_bytes()),
            neighbor.addr.port,
        ];

        tx.execute("UPDATE frontier SET peer_version = ?1, public_key = ?2, expire_block_height = ?3, last_contact_time = ?4, asn = ?5, org = ?6, allowed = ?7, denied = ?8, in_degree = ?9, out_degree = ?10, public = ?11 \
                    WHERE network_id = ?12 AND addrbytes = ?13 AND port = ?14", args)
            .map_err(db_error::SqliteError)?;

        if let Some(old_peer) = old_peer_opt {
            let slot_opt = Self::find_peer_slot(
                tx,
                neighbor.addr.network_id,
                &neighbor.addr.addrbytes,
                neighbor.addr.port,
            )?;
            if old_peer.public_key.to_bytes_compressed()
                != neighbor.public_key.to_bytes_compressed()
            {
                // this peer has re-keyed, so it might be a new peer altogether.
                // require it to re-announce its DBs
                if let Some(slot) = slot_opt {
                    debug!("Peer at slot {} changed; dropping its DBs", slot);
                    PeerDB::drop_stacker_dbs(tx, slot)?;
                }
            }
        }
        Ok(())
```
