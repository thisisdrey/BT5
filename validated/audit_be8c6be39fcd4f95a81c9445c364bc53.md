### Title
Unauthenticated public-key re-key via unverified `Handshake.node_public_key` in the `Some` branch of `validate_handshake` - ([File: stackslib/src/net/chat.rs])

### Summary
When a connection already has a bound public key (`self.connection.get_public_key() = Some(_)`), `validate_handshake`'s `Some(_)` branch (chat.rs:1072-1091) only checks the claimed address/port (and only for outbound connections), and never calls `message.verify_secp256k1(&handshake_data.node_public_key)` against the *new* key contained in the handshake payload. `update_from_handshake_data` (chat.rs:1131-1167) then unconditionally rebinds `self.connection`'s public key to whatever `handshake_data.node_public_key` was supplied, without any proof that the sender possesses the corresponding private key.

### Finding Description
The outer `StacksMessage` signature is verified against the connection's *currently bound* key inside `consume_payload_known_length` (`connection.rs:813-815`, `protocol.verify_payload_bytes(pubk, ...)` using `self.public_key`, the old key). This proves the sender controls the *old* key, not the new one embedded in the `HandshakeData.node_public_key` field.

`validate_handshake`'s two branches are asymmetric:
- `None` branch (chat.rs:1058-1071): calls `message.verify_secp256k1(&handshake_data.node_public_key)`, cryptographically proving possession of the *new* key's private key.
- `Some(_)` branch (chat.rs:1072-1091): does **not** call `verify_secp256k1` at all. It only enforces an address/port match, and only when `self.stats.outbound` is true; for inbound connections (or when `handshake_data.addrbytes.is_anynet()`), there is no check whatsoever in this branch.

After `validate_handshake` returns `Ok(())`, `handle_handshake` (chat.rs:1226-1258) calls `update_from_handshake_data`, which sets `self.connection.set_public_key(Some(pubk.clone()))` (chat.rs:1164) to the new, unverified key, and reports `updated = true`.

Critically, `handle_handshake` then, if `updated && self.stats.outbound` (chat.rs:1275-1295), opens a peerdb transaction and calls `Neighbor::load_and_update` / `neighbor.save_update`, **persisting the unverified new key to the peer database** as the neighbor's canonical public key.

Attack: An attacker completes one legitimate handshake with key A (satisfying the `None` branch's `verify_secp256k1`), binding the connection to A. The attacker then sends a second `Handshake` message whose `HandshakeData.node_public_key` field is set to an arbitrary key B (for which the attacker need not even hold the private key, or could use it for a Sybil/impersonation claim), while signing the outer `StacksMessage`/preamble with the original key A (satisfying `verify_payload_bytes` against the still-bound key A). Since the connection's key is `Some(A)`, `validate_handshake` takes the `Some(_)` branch, which does not verify B at all, only checks address/port (bypassable via `is_anynet()` on inbound, or trivially satisfiable on outbound since the attacker controls its own claimed addr/port fields). The handshake is accepted, `update_from_handshake_data` re-keys the connection to B, and if this is an outbound conversation, the peerdb record for that neighbor is rewritten to key B.

### Impact Explanation
An attacker can force a node to accept and persist an arbitrary, cryptographically-unproven public key as a peer's identity in its `peerdb` (via `Neighbor::load_and_update`/`save_update`), corrupting neighbor records used for peer authentication, walk/ranking, and future handshake validation. This is an unauthenticated write to persistent node state driven by a single crafted P2P message, repeatable per-connection and per-outbound-neighbor.

### Likelihood Explanation
Preconditions are modest and achievable by any unprivileged remote peer: complete one legitimate handshake (trivial, requires only a real keypair, which any attacker can generate) to become "known" to the node with a bound key, then send a second handshake with a different `node_public_key`. No secret, admin role, or privileged access is required; the P2P port is remotely reachable by design. The outbound-only peerdb-write path additionally requires the node to have connected outbound to the attacker (or the attacker's node to be an outbound neighbor of the victim), which is a normal, attacker-achievable configuration in a P2P Sybil scenario.

### Recommendation
In the `Some(_)` branch of `validate_handshake`, when `handshake_data.node_public_key` differs from `self.connection.get_public_key()`, require the same `message.verify_secp256k1(&handshake_data.node_public_key)` check performed in the `None` branch before allowing a re-key, in addition to (not instead of) the existing address/port checks.

### Proof of Concept
Rust test in `stackslib/src/net/chat.rs` test module:
1. Construct a `ConversationP2P` for an outbound peer and call `self.connection.set_public_key(Some(pubkey_a))` to simulate a prior legitimate handshake.
2. Craft a `StacksMessage` containing `StacksMessageType::Handshake(HandshakeData { node_public_key: pubkey_b_buffer, addrbytes: <matching self.peer_addrbytes>, port: <matching self.peer_port>, expire_block_height: <future>, .. })`, sign the message/preamble with private key A (`msg.sign(seq, &privkey_a)` or equivalent used elsewhere in tests), matching the currently bound key.
3. Call `self.validate_handshake(&local_peer, &chain_view, &mut msg)` and assert it returns `Ok(())`.
4. Call `self.update_from_handshake_data(&msg.preamble, &handshake_data)` and assert the returned `updated == true` and `self.connection.get_public_key() == Some(pubkey_b)`, despite the message never being verified against `pubkey_b`.