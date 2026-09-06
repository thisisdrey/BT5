### Title
`ConversationP2P::validate_handshake` skips signature verification against the current public key, letting replayed/forged handshakes downgrade a peer's authenticated key - ([File: stackslib/src/net/chat.rs])

### Summary
`validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` when `self.connection.get_public_key()` is `None` (first-time handshake). When a public key is already known for the conversation, the `Some(_)` branch performs only an outbound-address sanity check and never verifies the message signature against the currently trusted key, even though `handle_handshake`'s own doc-comment states a handshake "will only be accepted if we do not yet know the public key of this remote peer, or if it is signed by the current public key." Because this check is absent, any subsequent `Handshake` message — a captured replay of an earlier valid handshake, or even an unsigned/garbage-signed one on an inbound connection — is accepted and passed to `update_from_handshake_data`, which unconditionally calls `self.connection.set_public_key(Some(pubk))`.

### Finding Description
The intended invariant is: *current authenticated key == key that most recently produced a validly-signed handshake, verified against the previously-established key.* This is broken because: [1](#0-0) 

shows that when `self.connection.get_public_key()` returns `Some(_)`, the code path never calls `verify_secp256k1`. It only checks, for **outbound** connections, that the self-reported `addrbytes`/`port` match the known socket address — a check that is skipped entirely for inbound connections (`self.stats.outbound == false`). No cryptographic check ties the incoming `Handshake` to the peer that legitimately owns the current key.

`handle_handshake` then does: [2](#0-1) 
which calls `update_from_handshake_data`, and that function blindly overwrites the trusted key: [3](#0-2) 

There is no `seq`/nonce/replay check anywhere in `validate_handshake`, and — worse — no signature check at all against the current key once one has been established. So the scenario described (capture handshake #1's bytes before a re-key, then replay them after the peer legitimately re-keys to a new key with handshake #2) succeeds: the replayed message still carries a valid `secp256k1` signature (it was legitimately signed once), and since the `Some(_)` branch never re-checks the signature, `update_from_handshake_data` downgrades `self.connection`'s public key back to the old, now-revoked key. In fact, because there is no signature check at all in this branch, the vulnerability is broader than pure replay: on an inbound conversation, an attacker doesn't even need a previously-captured valid signature — any syntactically well-formed `Handshake` payload with a parseable `node_public_key` and non-expired `expire_block_height` that isn't the local peer's own key will be accepted, regardless of whether it is actually signed by anyone holding the claimed or current private key.

### Impact Explanation
An unprivileged remote party who can open a P2P connection to a node (or who already has an established, previously-authenticated conversation with it) can force that conversation's trusted public key to revert to a stale/attacker-chosen value by sending a single crafted or replayed `Handshake` message. This corrupts the node's notion of "who it is talking to" for that conversation — affecting subsequent authorization decisions that key off `ConversationP2P`'s public key (e.g., re-key bookkeeping via `Neighbor::load_and_update`/`save_update`, and any logic elsewhere in networking that trusts `get_public_key()` as an authenticated identity). This is repeatable per-message and requires no secret, no admin role, and no privileged position — only the ability to connect to the P2P port, matching the "authentication bypass" class of Critical impact.

### Likelihood Explanation
- Attacker needs only network reachability to the target's P2P port (no secrets, no privileged role).
- For the pure-replay path: the attacker must have captured an earlier legitimate handshake's bytes (passive on-path observation, e.g. from a prior direct connection to the victim, since P2P handshakes aren't encrypted) and the victim node must have since re-keyed while retaining the conversation object with the old key known.
- For the stronger forgery path on inbound connections, no captured legitimate message is even required — a garbage-signed `Handshake` with `data.addrbytes` irrelevant (since the address check is skipped for inbound) suffices, given the `Some(_)` branch's total absence of signature verification.
- Both paths are cheap, remotely triggerable, and repeatable indefinitely.

### Recommendation
In `validate_handshake`'s `Some(cur_pubk)` branch, require `message.verify_secp256k1(&handshake_data.node_public_key)` to succeed, **and** additionally require that either (a) the message is signed by `cur_pubk` (proving the existing key holder authorized the rotation), or (b) implement an explicit key-rotation proof (e.g., the new handshake must be signed by the currently trusted key, not merely self-signed by the new key) so that an unrelated party cannot present a new/old key without proof of continuity. Also add a monotonic `preamble.seq`/nonce or timestamp freshness check per conversation to reject stale replays independent of key-rotation logic.

### Proof of Concept
Extend the existing `stackslib::net::chat` handshake test flow (as used in `convo_handshake_self`-style tests):
1. Establish conversation A→B; perform handshake #1 with key `K1`, capture the exact serialized `StacksMessage` bytes.
2. Perform handshake #2 with a new key `K2` (simulating re-key); assert `convo_b.connection.get_public_key() == Some(K2)`.
3. Re-inject the captured handshake #1 bytes into `convo_b` via `handle_unauthenticated_control_message` (or the corresponding `chat`/`handle_handshake` call path).
4. Assert that `validate_handshake` returns `Ok(())` (no rejection) and that afterward `convo_b.connection.get_public_key() == Some(K1)`, proving the key was silently downgraded to the stale, revoked key by simply replaying old, still-signature-valid bytes — with no `seq`/current-key check preventing it.

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

**File:** stackslib/src/net/chat.rs (L1226-1251)
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

        let handshake_data = match message.payload {
            StacksMessageType::Handshake(ref mut data) => data.clone(),
            _ => panic!("Message is not a handshake"),
        };

        let old_pubkey_opt = self.connection.get_public_key();
        let updated = self.update_from_handshake_data(&message.preamble, &handshake_data)?;
```
