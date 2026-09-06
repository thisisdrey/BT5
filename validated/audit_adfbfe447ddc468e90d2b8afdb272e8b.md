### Title
Unauthenticated HandshakeAccept.node_public_key is trusted without signature verification, allowing forged PeerDB writes - ([File: stackslib/src/net/chat.rs], [File: stackslib/src/net/connection.rs])

### Summary
When a `NeighborWalk` sends an outbound `Handshake` and receives a `HandshakeAccept`/`StackerDBHandshakeAccept`, the responder's claimed `node_public_key` is adopted and persisted to `PeerDB` without ever cryptographically verifying that the sender possesses the private key corresponding to that public key. This differs from the inbound `Handshake` path, which explicitly calls `message.verify_secp256k1(&handshake_data.node_public_key)` in `validate_handshake`.

### Finding Description
At the connection layer, `consume_payload_known_length` (`stackslib/src/net/connection.rs:797-860`) only invokes `protocol.verify_payload_bytes(pubk, preamble, buf_bytes)` when `self.public_key` is already `Some(..)`: [1](#0-0) 
For the *first* response on a fresh outbound connection to an as-yet-unauthenticated neighbor (exactly the case in `NeighborWalk::handshake_try_finish` / `neighbor_handshakes_try_finish`), `self.public_key` is `None`, so this signature check is skipped entirely at the transport layer.

At the message-handling layer, `ConversationP2P::handle_handshake_accept` (`stackslib/src/net/chat.rs:1338-1391`) processes the `HandshakeAcceptData` payload by directly calling `update_from_handshake_data`, which sets `self.connection.set_public_key(Some(pubk))` straight from the attacker-controlled `handshake_accept.handshake.node_public_key`: [2](#0-1) [3](#0-2) 
No call to `StacksMessage::verify_secp256k1` or `Preamble::verify` appears anywhere in `handle_handshake_accept`. This is in stark contrast to the inbound `Handshake` path, `validate_handshake` (`stackslib/src/net/chat.rs:1047-1092`), which explicitly performs `message.verify_secp256k1(&handshake_data.node_public_key)` when no public key is yet known for the peer.

That accepted (and unverified) `HandshakeAcceptData` then flows into the neighbor-walk state machine: `NeighborWalk::handle_handshake_accept` (`stackslib/src/net/neighbors/walk.rs:676-737`) calls `self.neighbor_db.neighbor_from_handshake(...)` and `update_neighbor(...)`, which ultimately call `Neighbor::load_and_update` / `Neighbor::save_update` / `PeerDB::update_peer`/`try_insert_peer` (`stackslib/src/net/chat.rs:475-516`, `stackslib/src/net/neighbors/db.rs:507-539`, `stackslib/src/net/db.rs:1285-1334`) — persisting the attacker-chosen `pubkey` into PeerDB. Similarly, `neighbor_handshakes_try_finish` → `handle_neighbor_handshake_accept` → `add_or_schedule_replace_neighbor` (`stackslib/src/net/neighbors/walk.rs:1092-1124`, `stackslib/src/net/neighbors/db.rs:368-426`) inserts brand-new neighbor records from `HandshakeAccept` data with the same lack of signature verification.

The only mitigations present are: (1) `check_handshake_pubkey_hash` in the *pingback* path (`stackslib/src/net/neighbors/walk.rs:1663-1680`), which merely checks that the claimed pubkey's hash matches the `NeighborAddress.public_key_hash` we already recorded — this does not prove possession of the private key, it only checks self-consistency with previously-unverified data; and (2) the outbound address/port match check in `validate_handshake`, which is not exercised for `HandshakeAccept` messages at all since `validate_handshake` is only called from `handle_handshake` (inbound `Handshake` requests), not from `handle_handshake_accept`.

An attacker's exact message: connect to (or respond as) a peer that receives our outbound `Handshake`, and reply with a `HandshakeAccept` (or `StackerDBHandshakeAccept`) whose `handshake.node_public_key` is set to an arbitrary/unrelated public key, with the preamble signature either empty, garbage, or signed by a completely different key. Because `self.public_key` is `None` at this point and `handle_handshake_accept` never checks the signature against the claimed key, the message is accepted, and the fabricated pubkey is written into PeerDB.

### Impact Explanation
This results in an unauthenticated write to PeerDB (persistent node state): the victim's PeerDB frontier can contain a `Neighbor` record whose `public_key` field does not correspond to any key the counterparty can actually sign with. This forged identity can later be returned via `getneighbors`/GetNeighbors responses to other crawling peers, propagating a bogus (addr, pubkey) mapping across the network (relay-fanout / frontier poisoning), and can also cause legitimate outbound traffic addressed to that neighbor key to be misdirected or to fail authentication with the real owner of that address. This matches the "Critical: unauthenticated write to node state" category.

### Likelihood Explanation
- Attacker only needs to be a normal, unprivileged remote peer capable of accepting/responding to a P2P connection — no secrets, no privileged role.
- Precondition: the victim performs an outbound neighbor walk to the attacker's address (already the default crawling behavior) or contacts the attacker's advertised neighbor address during a neighbor-walk hop (`GetNeighborsBegin`/`GetHandshakesFinish` state).
- Cost is a single crafted `HandshakeAccept` message; fully repeatable on every handshake cycle and every neighbor-walk hop, and scriptable trivially.
- Remote reachability: yes, this is exactly the P2P handshake flow that all neighbor-discovery relies on.

### Recommendation
In `ConversationP2P::handle_handshake_accept` (and any other `HandshakeAcceptData`/`StackerDBHandshakeAccept` handling path), verify the message signature against the claimed `handshake.node_public_key` before trusting/persisting it — i.e., call something equivalent to `message.verify_secp256k1(&handshake_accept.handshake.node_public_key)` (mirroring `validate_handshake`'s behavior for inbound `Handshake` messages) prior to invoking `update_from_handshake_data` / `neighbor_db.update_neighbor` / `add_or_schedule_replace_neighbor`. This requires passing the full `StacksMessage` (not just the deserialized payload) into `handle_handshake_accept`, `NeighborWalk::handle_handshake_accept`, and `handle_neighbor_handshake_accept` so the preamble+payload bytes can be re-verified against the claimed key.

### Proof of Concept
Add a test in `stackslib/src/net/chat.rs` (or `stackslib/src/net/tests/neighbors.rs`) analogous to `convo_handshake_badsignature`:
1. Have `convo_1` send a signed `Handshake` to `convo_2`.
2. Instead of having `convo_2` reply normally, construct a `HandshakeAccept` payload with `handshake.node_public_key` set to an arbitrary freshly-generated `Secp256k1PublicKey` unrelated to `convo_2`'s actual private key, and set the message preamble's `signature` to `MessageSignature::empty()` or a signature from yet another unrelated key.
3. Feed this crafted message into `convo_1.chat(...)` (simulating receipt), then call `handle_handshake_accept` / drive `NeighborWalk::handshake_try_finish`.
4. Assert that `convo_1.connection.get_public_key()` was NOT updated to the forged key, and that `PeerDB::get_peer` for that neighbor address does **not** show the forged public key — this assertion should currently FAIL, demonstrating that `handle_handshake_accept` accepts the unverified key and `PeerDB` gets poisoned with `peerdb_entry.pubkey != cryptographically-proven-owner`.

### Citations

**File:** stackslib/src/net/connection.rs (L811-815)
```rust
        if buf_bytes.len() >= payload_len {
            // definitely have enough data to form a message
            if let Some(ref pubk) = self.public_key {
                protocol.verify_payload_bytes(pubk, preamble, buf_bytes)?;
            }
```

**File:** stackslib/src/net/chat.rs (L1131-1166)
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
```

**File:** stackslib/src/net/chat.rs (L1338-1345)
```rust
    fn handle_handshake_accept(
        &mut self,
        burnchain_view: &BurnchainView,
        preamble: &Preamble,
        handshake_accept: &HandshakeAcceptData,
        stackerdb_accept: Option<&StackerDBHandshakeData>,
    ) -> Result<(), net_error> {
        self.update_from_handshake_data(preamble, &handshake_accept.handshake)?;
```
