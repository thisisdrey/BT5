### Title
Pingback-handshake accept stores attacker-supplied `addrbytes`/`port` under a validated pubkey-hash without checking the actually-dialed address - (File: `stackslib/src/net/neighbors/walk.rs`)

### Summary
`check_handshake_pubkey_hash` (walk.rs:1664-1680) only verifies that `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key)` equals `naddr.public_key_hash`; it never compares `data.handshake.addrbytes`/`data.handshake.port` against the address the walk actually dialed (`naddr`). Immediately after this check passes, `pingback_handshakes_try_finish` calls `add_or_schedule_replace_neighbor` with `&data.handshake` (walk.rs:1713-1724), which contains the self-reported, attacker-controlled address fields, so those fields — not the dialed/verified `naddr` address — get persisted into the frontier.

### Finding Description
The pingback flow works as follows: `pingback_handshakes_begin` opens a TCP session to `naddr`'s address (walk.rs:1606-1613, via `neighbor_session_begin` → `neighbor_session_begin_only` in comms.rs, which pins the connection using `naddr.public_key_hash`). Once the reply arrives, `pingback_handshakes_try_finish` extracts `data: &HandshakeAcceptData` from the response (walk.rs:1692-1711) and constructs `peer_nk` using `message.to_neighbor_key(&data.handshake.addrbytes, data.handshake.port)` — i.e., using the *self-reported* address inside the payload, not the dialed `naddr` (walk.rs:1713).

`check_handshake_pubkey_hash(&peer_nk, data, &naddr)` (walk.rs:1714, defined 1664-1680) validates only the public-key hash:
```
let neighbor_pubkey_hash = Hash160::from_node_public_key_buffer(&data.handshake.node_public_key);
if neighbor_pubkey_hash != naddr.public_key_hash { return false; }
true
```
There is no equivalent check that `data.handshake.addrbytes == naddr.addrbytes` and `data.handshake.port == naddr.port`. If the check passes, `self.neighbor_db.add_or_schedule_replace_neighbor(network, &message.preamble, &data.handshake, db_data, ...)` is called (walk.rs:1718-1724), passing the entire self-reported `HandshakeData` (including its address fields) into the persistence path.

Because the whole point of the "pingback" mechanism is to establish trust in a self-reported address by dialing it and getting back a correctly-keyed response, the code implicitly assumes that whoever answers on that TCP connection is also the authoritative source for what address should be recorded. But nothing forces `data.handshake.addrbytes/port` in the *reply payload* to equal the socket that was dialed — those are just wire-controlled struct fields the remote peer fills in arbitrarily. A peer that legitimately owns its own key and its own reachable address `naddr` (so the pubkey-hash check passes) can still set the `addrbytes`/`port` inside its `HandshakeAcceptData` to any third-party address `V`, and that spoofed value — not the verified `naddr` — is what gets written to the frontier via `add_or_schedule_replace_neighbor`. [1](#0-0) [2](#0-1) 

### Impact Explanation
An unprivileged remote peer that can connect inbound and complete one pingback round-trip can cause the victim node to persist a `Neighbor` record whose `addrbytes`/`port` are attacker-chosen and unrelated to the address that was actually cryptographically verified via the TCP dial. Because `NeighborAddress`/`Neighbor` records are gossiped further in `Neighbors`/`NeighborAddresses` replies, this forged address-to-pubkey association can propagate to other peers in the network, matching the "network-wide propagation of forged data" / "unauthenticated write to state" Critical category. Downstream effects include future reconnection attempts (by this node or peers that ingest the gossiped record) being redirected toward the attacker-chosen `addr:port`, which can be used to direct connection attempts at arbitrary third-party hosts (SSRF-like redirection) or otherwise pollute peer discovery data with false address bindings.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to (1) get itself queued as a `pending_pingback_handshakes` candidate (i.e., have connected inbound and been considered for pingback verification, which is routine unauthenticated peer behavior), and (2) respond to the resulting outbound pingback probe with a `HandshakeAccept`/`StackerDBHandshakeAccept` whose `node_public_key` hashes to the expected `naddr.public_key_hash` (trivial, since the attacker legitimately owns that key already) but whose `addrbytes`/`port` fields are set to an arbitrary value. This is a single crafted message, fully repeatable, and requires no privileged role, secret, or victim compromise.

### Recommendation
In `check_handshake_pubkey_hash` (or immediately before calling `add_or_schedule_replace_neighbor`), also verify that `data.handshake.addrbytes == naddr.addrbytes` and `data.handshake.port == naddr.port` (or explicitly use `naddr`'s dialed address/port rather than the self-reported `data.handshake` address/port when constructing the record passed to `add_or_schedule_replace_neighbor`). Only trust self-reported address fields from `HandshakeData` for peers that have not yet been pingback-verified; for the pingback-confirmation path specifically, the persisted address should come from the connection that was dialed (`naddr`), not from attacker-controlled payload fields.

### Proof of Concept
Add a test in `stackslib/src/net/neighbors/walk.rs`'s test module (or `net/tests/neighbors.rs`) that:
1. Constructs a `naddr: NeighborAddress` with `addrbytes = V` (a "victim" IP) and `public_key_hash = H` derived from an attacker-controlled keypair.
2. Simulates `pingback_handshakes_try_finish` receiving a `StacksMessageType::HandshakeAccept` whose `data.handshake.node_public_key` hashes to `H` (passes `check_handshake_pubkey_hash`) but whose `data.handshake.addrbytes/port` are set to the attacker's real address `A` (or any third value), while the mocked/dialed connection was opened to `naddr` (`V`).
3. Call `add_or_schedule_replace_neighbor` via the normal flow and then inspect the resulting `PeerDB`/frontier entry.
4. Assert that the persisted `Neighbor.addr`/`port` equals `A` (attacker-chosen) rather than `V` (the address that was actually dialed and cryptographically bound to `H`), demonstrating that no equality check ties the persisted address back to `naddr`.

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L1663-1680)
```rust
    /// Does a given handshakedata represent an expected public key hash?
    fn check_handshake_pubkey_hash(
        nk: &NeighborKey,
        data: &HandshakeAcceptData,
        naddr: &NeighborAddress,
    ) -> bool {
        let neighbor_pubkey_hash =
            Hash160::from_node_public_key_buffer(&data.handshake.node_public_key);
        if neighbor_pubkey_hash != naddr.public_key_hash {
            debug!(
                "Neighbor {:?} had an unexpected pubkey hash: expected {:?} != {:?}",
                nk, &naddr.public_key_hash, &neighbor_pubkey_hash
            );
            return false;
        }

        true
    }
```

**File:** stackslib/src/net/neighbors/walk.rs (L1690-1724)
```rust
        for (naddr, message) in self.comms.collect_replies(network).into_iter() {
            // if we got back a HandshakeAccept, and it's on the same chain as us, we're good!
            let (data, db_data) = match message.payload {
                StacksMessageType::HandshakeAccept(ref data) => {
                    debug!("{:?}: received HandshakeAccept from peer {:?}; now known to be routable from us", network.get_local_peer(), &message.to_neighbor_key(&data.handshake.addrbytes, data.handshake.port));
                    (data, None)
                }
                StacksMessageType::StackerDBHandshakeAccept(ref data, ref db_data) => {
                    debug!("{:?}: received StackerDBHandshakeAccept from peer {:?}; now known to be routable from us", network.get_local_peer(), &message.to_neighbor_key(&data.handshake.addrbytes, data.handshake.port));
                    (data, Some(db_data))
                }
                _ => {
                    let nkey = naddr.to_neighbor_key(network);
                    debug!(
                        "{:?}: Neighbor {:?} replied {:?} instead of pingback handshake",
                        network.get_local_peer(),
                        &nkey,
                        &message.get_message_name()
                    );
                    continue;
                }
            };

            let peer_nk = message.to_neighbor_key(&data.handshake.addrbytes, data.handshake.port);
            if !Self::check_handshake_pubkey_hash(&peer_nk, data, &naddr) {
                continue;
            }

            self.neighbor_db.add_or_schedule_replace_neighbor(
                network,
                &message.preamble,
                &data.handshake,
                db_data,
                &mut self.neighbor_replacements,
            )?;
```
