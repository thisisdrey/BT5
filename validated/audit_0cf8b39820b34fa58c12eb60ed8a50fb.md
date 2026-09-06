### Title
Unauthenticated re-key of an established P2P connection via a second `Handshake` message with no signature verification - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` when the connection has no public key bound yet (`self.connection.get_public_key() == None`). Once a connection is already keyed (`Some(_)`), the function performs **no signature check whatsoever** — it only re-checks the self-reported address, and only for outbound connections. This lets a remote peer that has completed one handshake send a second, unsigned/mis-signed `Handshake` claiming an arbitrary `node_public_key`, which `update_from_handshake_data` then unconditionally adopts.

### Finding Description
In `validate_handshake` (chat.rs:1052-1092):
```rust
match self.connection.get_public_key() {
    None => {
        message.verify_secp256k1(&handshake_data.node_public_key)... // signature IS checked
    }
    Some(_) => {
        if self.stats.outbound && (... address mismatch ...) {
            return Err(net_error::InvalidHandshake);
        }
        // NOTE: no verify_secp256k1 call here at all
    }
};
``` [1](#0-0) 

The `Some(_)` branch (executed for every subsequent handshake once a key is bound) never calls `message.verify_secp256k1`. It only rejects on address mismatch, and that check is gated on `self.stats.outbound`, so it is skipped entirely for inbound connections. The remaining checks in `validate_handshake` — public-key format validity (`to_public_key()`), expiry, and "not equal to our own key" — do not verify that the message was actually signed by the private key corresponding to `handshake_data.node_public_key`, nor by the previously-authenticated key.

After `validate_handshake` returns `Ok(())`, `update_from_handshake_data` (chat.rs:1131-1167) is called and unconditionally does:
```rust
self.connection.set_public_key(Some(pubk.clone()));
``` [2](#0-1) 

So an inbound peer that has completed handshake A can send a second `Handshake` message containing `node_public_key = B` (an arbitrary key of the attacker's choosing, or even garbage that merely deserializes as a valid compressed pubkey) with an incorrect/irrelevant signature, and the conversation will silently rebind to key B with no proof that the sender controls the private key for B, nor any proof it still controls the private key for A. The equality the connection should preserve — "the bound key always corresponds to a signature the peer actually produced" — is broken in the re-key path.

### Impact Explanation
This is an authentication-bypass / identity-hijack primitive on the P2P layer: a remote unprivileged peer can, after one legitimate handshake, silently swap the identity bound to its conversation to any key it names, with no cryptographic proof of key possession for inbound sessions. Any subsequent trust decisions keyed off `get_public_key()` (e.g., peer identity-based authorization, StackerDB slot ownership checks that rely on conversation identity, neighbor-list bookkeeping) can be misled into treating the connection as belonging to a different, attacker-chosen identity. This matches the "auth bypass" Critical category since it is a single-message, repeatable identity confusion on an already-established connection.

### Likelihood Explanation
Preconditions: the attacker must be able to open a P2P connection and complete one legitimate handshake with its own real key (trivial, since anyone can connect and handshake) so that `get_public_key()` becomes `Some(_)`. Then a second crafted `Handshake` message with an arbitrary `node_public_key` field is sufficient. No secrets, no privileged role, and no valid signature over the new key are required for inbound connections (the address re-check that provides any resistance only applies when `self.stats.outbound` is true). This is fully remote, requires only two messages, and is repeatable at will.

### Recommendation
In `validate_handshake`, always verify `message.verify_secp256k1(&handshake_data.node_public_key)` regardless of whether `get_public_key()` is `None` or `Some(_)`, so every handshake — initial or re-key — proves possession of the private key for the newly claimed `node_public_key`. Optionally also require that a re-key be signed by (or otherwise consistent with) the previously bound key to prevent unrelated-identity takeover of an already-authenticated session.

### Proof of Concept
Extend the existing `net::chat` handshake test harness (pattern of `convo_handshake_selfvalidate`):
1. Establish `ConversationP2P` as inbound, complete `validate_handshake`/`update_from_handshake_data` with key A's correctly signed `Handshake` — assert `self.connection.get_public_key() == Some(pub_A)`.
2. Build a second `StacksMessage` with `StacksMessageType::Handshake(HandshakeData { node_public_key: pub_B, .. })`, but sign it with an unrelated private key `C` (or leave the `MessageSignature` as garbage bytes) — i.e., the signature does NOT match `pub_B`.
3. Call `convo.validate_handshake(&local_peer, &chain_view, &mut msg)` and observe it returns `Ok(())` because the `Some(_)` branch never calls `verify_secp256k1`.
4. Call `convo.update_from_handshake_data(&preamble, &handshake_data)` and assert `self.connection.get_public_key() == Some(pub_B)`, proving the conversation re-keyed to an attacker-chosen identity without any valid signature over `pub_B` or proof of continued control of key A.

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
