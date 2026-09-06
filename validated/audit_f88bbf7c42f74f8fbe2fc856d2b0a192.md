### Title
Unauthenticated peer identity replacement via inbound Handshake re-key with no signature-continuity check - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` when `self.connection.get_public_key()` is `None`. Once a public key is already known for the conversation, the `Some(_)` branch performs **no signature check at all** — it only re-validates address/port, and only for outbound connections. This lets a remote peer on an already-handshaked inbound connection send a new `Handshake` payload carrying an arbitrary, unrelated `node_public_key`, with no proof that the sender controls the previously-established key, and have `handle_handshake` silently call `update_from_handshake_data` to overwrite the conversation's stored identity.

### Finding Description
The claimed invariant — "peer identity update stored (`update_from_handshake_data`) == identity change authenticated by the peer's previously-established key" — is broken.

Code path: `stackslib/src/net/chat.rs`
```
fn validate_handshake(...) {
    ...
    match self.connection.get_public_key() {
        None => {
            message.verify_secp256k1(&handshake_data.node_public_key)...   // only checked here
        }
        Some(_) => {
            if self.stats.outbound && (...addr/port mismatch...) {
                return Err(net_error::InvalidHandshake);
            }
            // NOTE: no verify_secp256k1 call anywhere in this branch
        }
    };
    ...
    Ok(())
}
``` [1](#0-0) 

For an **inbound** conversation (`self.stats.outbound == false`), the address/port check is skipped entirely as well, meaning the `Some(_)` branch performs *zero* validation of the new handshake's authenticity relative to the previously stored key. `StacksMessage::consensus_deserialize` performs no signature verification during wire deserialization [2](#0-1) , and `verify_secp256k1` is only invoked where explicitly called [3](#0-2) . Since it's never called in the `Some(_)` branch, an attacker can put any signature bytes and any freshly-generated public key into the `Handshake` payload.

`handle_handshake` then trusts the passing `validate_handshake` result and calls `update_from_handshake_data`, which detects `pubk != cur_pubk`, sets `updated = true`, and unconditionally overwrites the stored identity via `self.connection.set_public_key(Some(pubk.clone()))` — with no requirement that the new key was ever proven to be controlled by the same party that established the original identity: [4](#0-3) [5](#0-4) 

The function's own doc comment states the intended security property: "A handshake will only be accepted if we do not yet know the public key of this remote peer, or if it is signed by the current public key" [6](#0-5) , but the implementation never enforces the "signed by the current public key" half of that statement.

Note: the codebase's own re-key regression test (`convo_handshake_update_key`) signs the re-key `Handshake` with the **old** private key [7](#0-6) , i.e. the test suite assumes/exercises the "signed by current key" continuity property, but the shipped `validate_handshake` code does not actually check for it — the same test would pass even if signed with a brand-new unrelated key, because nothing in the `Some(_)` branch inspects the signature.

### Impact Explanation
An attacker who has completed a legitimate handshake with a victim node (trivial — anyone can connect and handshake) can subsequently send one more crafted `Handshake` message over the same connection with a freshly generated private key and no proof of continuity. This causes:
- The conversation's stored identity (`connection.public_key`) for that `NeighborKey`/socket to be silently replaced (identity hijack on a live connection), affecting any logic keyed on the conversation's known peer identity (e.g., reputation/allow-deny decisions, StackerDB replication partner identity `update_from_stacker_db_handshake_data`, relay bookkeeping).
- For outbound conversations, `updated && self.stats.outbound` additionally triggers a PeerDB write (`Neighbor::load_and_update` + `neighbor.save_update`), persisting the attacker-chosen key into the peer database for that neighbor entry — an unauthenticated write to persisted state.
Repeatable per message; requires only stock TCP connect + one extra `Handshake` send. Matches the "Critical - unauthenticated/unauthorized write to state" category.

### Likelihood Explanation
Preconditions: attacker only needs to be able to open a P2P TCP connection to the victim (any unprivileged remote party) and complete an initial handshake (the normal, unauthenticated bootstrap flow already supported). No secrets, no privileged role, no valid old private key needed. Cost is negligible — one extra signed message with a self-generated keypair. Fully remotely reachable on the P2P port, and repeatable indefinitely (attacker can keep re-keying).

### Recommendation
In `validate_handshake`'s `Some(_)` branch, require that the incoming `Handshake` message is signed either by the currently-known public key (proving continuity/authorized re-key) or, if signed by the new key, require an additional binding (e.g., a signature over the new key by the old key, or vice versa) before accepting the identity change. At minimum, always verify `message.verify_secp256k1` against the OLD stored key when a key is already known, and only allow updating to a genuinely new key through an explicit, separately-authenticated re-key protocol step rather than accepting whatever key appears in a subsequently received `Handshake`.

### Proof of Concept
Rust test plan (extending existing `convo_handshake_update_key`-style tests in `stackslib/src/net/chat.rs`):
1. Set up `convo_1`/`convo_2` and perform the normal handshake so both sides learn each other's public keys (`connection.get_public_key()` is `Some(...)`), mirroring lines 4380-4428.
2. Instead of using `old_peer_1_privkey` to sign the re-handshake (as the existing test does), generate a brand-new random private key `attacker_privkey = Secp256k1PrivateKey::random()` with **no relation** to `old_peer_1_privkey`, build `HandshakeData` with `node_public_key` derived from `attacker_privkey`, and sign the message with `attacker_privkey` itself (i.e., self-consistent signature/key pair, but unrelated to the previously-established key).
3. Send this over the same live inbound TCP connection to `convo_2` (`convo_1.sign_message(&chain_view, &attacker_privkey, StacksMessageType::Handshake(handshake_data_1))`, then `send_signed_request` + `convo_send_recv` + `convo_2.chat(...)`).
4. Assert that `convo_2.chat(...)` returns success (no `InvalidHandshake`/`InvalidMessage` error) and that `convo_2.connection.get_public_key().unwrap()` now equals `Secp256k1PublicKey::from_private(&attacker_privkey)` instead of the original `local_peer_1` key — demonstrating `update_from_handshake_data` replaced the trusted identity with zero proof of continuity with the previously-established key.

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

**File:** stackslib/src/net/chat.rs (L1149-1167)
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
    }
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

**File:** stackslib/src/net/chat.rs (L1245-1258)
```rust
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
```

**File:** stackslib/src/net/chat.rs (L4438-4446)
```rust
        let handshake_data_1 = HandshakeData::from_local_peer(&local_peer_1);
        let handshake_1 = convo_1
            .sign_message(
                &chain_view,
                &old_peer_1_privkey,
                StacksMessageType::Handshake(handshake_data_1),
            )
            .unwrap();

```

**File:** stackslib/src/net/codec.rs (L1346-1363)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<StacksMessage, codec_error> {
        let preamble: Preamble = read_next(fd)?;
        if preamble.payload_len > MAX_MESSAGE_LEN - PREAMBLE_ENCODED_SIZE {
            return Err(codec_error::DeserializeError(
                "Message would be too big".to_string(),
            ));
        }

        let relayers: Vec<RelayData> = read_next_at_most::<_, RelayData>(fd, MAX_RELAYERS_LEN)?;
        let payload: StacksMessageType = read_next(fd)?;

        let message = StacksMessage {
            preamble,
            relayers,
            payload,
        };
        Ok(message)
    }
```

**File:** stackslib/src/net/codec.rs (L1487-1502)
```rust
    /// Verify this message by treating the public key buffer as a secp256k1 public key.
    /// Fails if:
    /// * the signature doesn't match
    /// * the buffer doesn't encode a secp256k1 public key
    pub fn verify_secp256k1(&self, public_key: &StacksPublicKeyBuffer) -> Result<(), net_error> {
        let secp256k1_pubkey = public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;

        let mut message_bits = vec![];
        self.relayers.consensus_serialize(&mut message_bits)?;
        self.payload.consensus_serialize(&mut message_bits)?;

        let mut p = self.preamble.clone();
        p.verify(&message_bits, &secp256k1_pubkey).map(|_m| ())
    }
```
