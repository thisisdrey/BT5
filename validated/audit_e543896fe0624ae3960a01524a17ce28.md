### Title
Unauthenticated Peer Rekey via Second `Handshake` Bypassing Signature Check When `get_public_key()` Is `Some` - ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P::validate_handshake` only calls `message.verify_secp256k1(&handshake_data.node_public_key)` when `self.connection.get_public_key()` is `None` (first handshake). When a public key is already established (`Some(_)`), the function performs **no signature verification at all** — it only performs an address-match sanity check, and that check is skipped entirely for `self.stats.outbound == false` (inbound connections) or when `handshake_data.addrbytes.is_anynet()` is true. Because `update_from_handshake_data` unconditionally calls `self.connection.set_public_key(Some(pubk.clone()))`, an attacker who already completed a handshake can send a second `Handshake` claiming an arbitrary new public key and have it silently accepted, overwriting the previously authenticated identity.

### Finding Description
In `stackslib/src/net/chat.rs`, `validate_handshake` (lines 1047-1127):

```rust
match self.connection.get_public_key() {
    None => {
        message.verify_secp256k1(&handshake_data.node_public_key)...?;
    }
    Some(_) => {
        // for outbound connections, the self-reported address must match socket address...
        if self.stats.outbound
            && (!handshake_data.addrbytes.is_anynet()
                && (self.peer_addrbytes != handshake_data.addrbytes
                    || self.peer_port != handshake_data.port))
        { return Err(net_error::InvalidHandshake); }
    }
};
``` [1](#0-0) 

The `Some(_)` branch never calls `verify_secp256k1`, so there is no cryptographic proof that the sender controls either the old established key or the newly claimed `handshake_data.node_public_key`. The only gate is an address/port equality check, and that gate is (a) entirely skipped for inbound connections (`self.stats.outbound` is `false`), and (b) skippable for outbound connections by setting `addrbytes` to the any-net bind address (`0.0.0.0`/`::`), per `is_anynet()`. All other checks in `validate_handshake` (public-key format, `expire_block_height`, "not from myself") do not verify that the sender is the legitimate holder of the previously-authenticated key.

After `validate_handshake` returns `Ok(())`, `update_from_handshake_data` (lines 1131-1167) unconditionally overwrites the stored key:
```rust
self.connection.set_public_key(Some(pubk.clone()));
``` [2](#0-1) 

Attacker flow: complete an initial handshake normally (establishing `stored_pubkey = K1`, `is_authenticated = true`). Then send a second `Handshake` message signed with an unrelated private key `K2`, with `handshake_data.node_public_key = K2_pub` and `addrbytes` set to `0.0.0.0` (or, for inbound sockets, any address at all since the check doesn't apply). `validate_handshake` reaches the `Some(_)` branch, the address condition is false (bypassed via anynet or inapplicable for inbound), so it returns `Ok(())` without ever checking that the message was signed by `K1` or `K2`. `update_from_handshake_data` then swaps `stored_pubkey` from `K1` to `K2_pub`, breaking the invariant that the conversation's stored public key corresponds to the key that actually authenticated the socket.

### Impact Explanation
This breaks peer-identity integrity for an already-authenticated P2P session: the node's record of "who it is talking to" can be silently swapped by the remote party without proving possession of any consistent key. This enables identity confusion/unauthorized rekey of an existing session — matching the Critical category of "unauthenticated/unauthorized write to state" (the in-memory peer identity/session state, and downstream data such as `peer_addrbytes`/`data_url`/services fields that get updated from `update_from_handshake_data`). It also potentially disrupts NeighborWalk/PeerDB bookkeeping keyed on the peer's public key, since subsequent operations trust `self.connection.get_public_key()` as this peer's identity.

### Likelihood Explanation
Preconditions: attacker needs only to be a normal remote peer capable of completing one initial handshake (unprivileged, remotely reachable P2P port) and then sending a second `Handshake` message on the same conversation/socket — no secret, no privileged role, no special peer/config state. Cost is a single additional message; the attack is repeatable at will. This does not require breaking any signature/crypto, only reaching the code path that omits the check.

### Recommendation
In the `Some(_)` branch of `validate_handshake`, require the incoming `Handshake` message to be verified with `message.verify_secp256k1` against either the currently stored public key (to authenticate a legitimate protocol continuation) or, if genuinely rekeying, require it to be signed by the *old* stored key to authorize the transition to the new key. Do not rely solely on the address-match heuristic (which is not a substitute for authentication and is bypassable via `is_anynet()` or simply inapplicable to inbound connections).

### Proof of Concept
Rust test plan in `stackslib/src/net/chat.rs` test module:
1. Set up `convo_1`/`convo_2` as in existing handshake tests; complete a full handshake with `convo_2`'s key `K1`, asserting `convo_1.connection.get_public_key() == Some(K1_pub)`.
2. Construct a new `StacksMessage` of type `Handshake` with `handshake_data.node_public_key = K2_pub` (a different keypair) and `handshake_data.addrbytes = PeerAddress::from_ipv4(0,0,0,0,0)` (any-net), sign it with `K2`'s private key, and send it into `convo_1.chat(...)`.
3. Assert that `validate_handshake` returns `Ok(())` (or that `chat()` does not return `Err(net_error::InvalidHandshake)`), and that after processing, `convo_1.connection.get_public_key() == Some(K2_pub)` — i.e., the previously-authenticated key `K1_pub` was silently replaced without any signature proof tying `K2` to the existing session.

### Citations

**File:** stackslib/src/net/chat.rs (L1058-1091)
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
