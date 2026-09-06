### Title
Unauthenticated re-key of an established inbound P2P conversation via `ConversationP2P::validate_handshake` - (File: stackslib/src/net/chat.rs)

### Summary
When `self.connection.get_public_key()` is already `Some(old_key)`, `validate_handshake` only re-checks the claimed `addrbytes`/`port` against the socket peer, and only for outbound conversations. For inbound conversations (`self.stats.outbound == false`), the `Some(_)` match arm performs no address check and, critically, no signature re-verification of the new `handshake_data.node_public_key` at all. `update_from_handshake_data` then unconditionally calls `self.connection.set_public_key(Some(pubk))` with whatever key was in the second Handshake payload, returning `updated = true`.

### Finding Description
The claimed invariant is: *the public key bound to a `ConversationP2P`/`NeighborKey` slot equals the identity that authenticated the original accepted handshake session*. `validate_handshake` (stackslib/src/net/chat.rs:1047-1127) breaks this for inbound connections:

- `None` branch (first handshake): explicitly calls `message.verify_secp256k1(&handshake_data.node_public_key)` (chat.rs:1062-1070) to prove the sender holds the claimed key.
- `Some(_)` branch (re-handshake, key already on file): the only check is the outbound address/port match, gated by `self.stats.outbound` (chat.rs:1079-1090). For an inbound conversation this whole `if` is skipped, so **no signature check against either the old key or the newly-claimed `node_public_key` occurs in this function at all**. The rest of `validate_handshake` only validates key format, expiry, and that the key isn't the local node's own key — none of which prevent an attacker-chosen arbitrary new key.

The dispatching caller, `handle_handshake` (chat.rs:1214-1296), only gates on the `authenticated` flag when `self.connection.options.disable_inbound_handshakes` is set; by default this option does not block unauthenticated inbound handshakes, so the flow proceeds to `validate_handshake` → `update_from_handshake_data` regardless.

`update_from_handshake_data` (chat.rs:1131-1167) compares `pubk` (parsed from the new message) to `cur_pubk_opt` and, if different, sets `updated = true` and calls `self.connection.set_public_key(Some(pubk.clone()))` (chat.rs:1164) — with no re-verification tying the wire signature to the freshly-claimed key, and no re-check of the peer's original address/session binding for inbound peers.

Net effect: an attacker who completed an original inbound handshake as key A can send a second Handshake claiming a different `node_public_key` B, and the conversation's bound identity flips to B, with `self.connection.get_public_key() == Some(B)` and no addrbytes recheck — an in-place identity swap on an already-accepted inbound session.

### Impact Explanation
This is an authentication/identity-binding bypass on an established inbound `ConversationP2P`. A remote peer can, at will, change which public key the node associates with an inbound `NeighborKey` slot without re-proving that identity through a fresh, address-validated handshake. Downstream code that trusts `self.connection.get_public_key()` for authorization decisions on this conversation (e.g., relay-permission checks, StackerDB slot-owner checks tied to conversation identity, neighbor trust bookkeeping) would then operate under the attacker-chosen identity B. This matches the "auth bypass" category under Critical impact, since it is a single-message, remotely triggerable violation of the peer-identity invariant on an existing authenticated session.

### Likelihood Explanation
Preconditions: attacker only needs to be able to open one inbound P2P connection and complete a normal handshake (trivial, unprivileged, remotely reachable). No RPC secret, no privileged role, no local access needed. The second Handshake message is ordinary wire traffic already accepted by the protocol; `disable_inbound_handshakes` defaults to allowing inbound handshakes to be processed. The action is repeatable (attacker can re-key the same conversation slot repeatedly to different identities).

### Recommendation
In `validate_handshake`, when `self.connection.get_public_key()` is `Some(cur_pubk)` and the new `handshake_data.node_public_key` differs from `cur_pubk`, require the message to be verified against the *new* claimed key via `message.verify_secp256k1(&handshake_data.node_public_key)` (as already done in the `None` branch), and additionally re-validate the peer's address/session binding (not just for outbound) before allowing `update_from_handshake_data` to switch keys on an existing conversation slot.

### Proof of Concept
Add a test in `stackslib/src/net/chat.rs` test module:
1. Construct `convo_2` as an inbound `ConversationP2P` (`stats.outbound = false`).
2. Perform an initial Handshake signed by key `A`; call `validate_handshake` then `update_from_handshake_data`, assert `self.connection.get_public_key() == Some(A)`.
3. Build a second `StacksMessage::Handshake` with `handshake_data.node_public_key = B` (a different keypair), sign the message with `B`'s private key via `StacksMessage::sign` (i.e., attacker legitimately controls B).
4. Call `convo_2.validate_handshake(...)` — expect `Ok(())` since the `Some(_)` inbound branch performs no check.
5. Call `convo_2.update_from_handshake_data(&preamble, &handshake_data)` — assert it returns `Ok(true)` (`updated == true`) and `convo_2.connection.get_public_key() == Some(B)`, with `convo_2.peer_addrbytes`/`peer_port` never re-validated against B's claims.