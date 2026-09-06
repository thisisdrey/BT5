### Title
`ConversationP2P::validate_handshake` never compares `message.preamble.network_id` to `local_peer.network_id` before `handle_handshake` commits a `Neighbor` row via `load_and_update` - (File: stackslib/src/net/chat.rs)

### Finding Description
`validate_handshake` (chat.rs:1047-1127) performs signature verification, public-key sanity, expiry, and self-handshake checks, but at no point compares `message.preamble.network_id` against `local_peer.network_id`. `update_from_handshake_data` (chat.rs:1129-1167) then unconditionally copies the untrusted value: `self.peer_network_id = preamble.network_id;` (chat.rs:1142). In `handle_handshake` (chat.rs:1214-1296), after `validate_handshake` returns `Ok`, the code calls:
```
Neighbor::load_and_update(&tx, message.preamble.peer_version, message.preamble.network_id, &handshake_data)?;
neighbor.save_update(&tx, None)?;
tx.commit()...
```
(chat.rs:1278-1286), directly persisting a `PeerDB` row keyed on the attacker-supplied `preamble.network_id`, not on any value cross-checked against the node's own configured `local_peer.network_id`. I fully read both `validate_handshake` and `handle_handshake` end-to-end and found no such equality check anywhere in this path.

### Impact Explanation
If this gate is indeed absent from the full message-dispatch pipeline (not just these two functions), a peer on a different network (e.g., testnet) could get a frontier/neighbor entry committed into a mainnet node's `PeerDB` under a foreign `network_id`, corrupting the node's neighbor table and potentially causing it to treat/relay data from the wrong chain's peers. This would match a Critical "unauthenticated write to state" impact if reachable.

### Likelihood Explanation
I could not fully rule out that an earlier stage in the message-reception/dispatch pipeline (outside `validate_handshake`/`handle_handshake`, e.g. a general preamble/network_id gate in `chat()` or in connection-level message validation) rejects mismatched `network_id` before `handle_handshake` is ever invoked. My tool budget was exhausted before I could locate or rule out such an earlier check elsewhere in `chat.rs` (grep results indicated ~40+ occurrences of `network_id` in the file, but I was only able to read and confirm the absence of the check within the specific `validate_handshake`/`handle_handshake`/`update_from_handshake_data` functions cited by the question).

### Recommendation
Add an explicit check in `validate_handshake` (or earlier in message dispatch) that rejects the handshake (`net_error::InvalidHandshake`) when `message.preamble.network_id != local_peer.network_id`, before any `update_from_handshake_data` or `Neighbor::load_and_update` call.

### Proof of Concept
Cannot be fully substantiated without confirming whether an earlier network_id gate exists elsewhere in the dispatch path; this requires further verification (e.g., a Devin session with full-repo access) to grep the complete `chat.rs` for any `network_id` comparison outside the functions cited, and to write a test that sends a `Handshake` with `preamble.network_id` differing from `local_peer.network_id` and asserts whether `handle_handshake` returns `HandshakeReject` prior to any DB write. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** stackslib/src/net/chat.rs (L1214-1296)
```rust
    fn handle_handshake(
        &mut self,
        network: &mut PeerNetwork,
        message: &mut StacksMessage,
        authenticated: bool,
        ibd: bool,
    ) -> Result<(Option<StacksMessage>, bool), net_error> {
        if !authenticated && self.connection.options.disable_inbound_handshakes {
            debug!("{:?}: blocking inbound unauthenticated handshake", &self);
            return Ok((None, true));
        }

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

        let handshake_data = match message.payload {
            StacksMessageType::Handshake(ref mut data) => data.clone(),
            _ => panic!("Message is not a handshake"),
        };

        let old_pubkey_opt = self.connection.get_public_key();
        let updated = self.update_from_handshake_data(&message.preamble, &handshake_data)?;
        let _authentic_msg = if !updated {
            "same"
        } else if old_pubkey_opt.is_none() {
            "new"
        } else {
            "upgraded"
        };

        debug!("Handling handshake";
             "neighbor" => ?self,
             "authentic_msg" => &_authentic_msg,
             "public_key" => &to_hex(
                &handshake_data
                    .node_public_key
                    .to_public_key()
                    .unwrap()
                    .to_bytes_compressed()
             ),
             "services" => &to_hex(&handshake_data.services.to_be_bytes()),
             "expires_block_height" => handshake_data.expire_block_height,
             "supports_mempool_query" => Self::supports_mempool_query(handshake_data.services),
        );

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
