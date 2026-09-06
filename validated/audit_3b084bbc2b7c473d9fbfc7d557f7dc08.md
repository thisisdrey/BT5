### Title
Missing pubkey-hash verification in `neighbor_handshakes_try_finish` allows unauthenticated identity write to frontier - (stackslib/src/net/neighbors/walk.rs)

### Summary
`check_handshake_pubkey_hash` (stackslib/src/net/neighbors/walk.rs:1664) verifies that a `HandshakeAcceptData`'s embedded public key hashes to the `NeighborAddress.public_key_hash` that was gossiped for that peer. This check is invoked on the inbound/pingback path (`pingback_handshakes_try_finish`, walk.rs:1714) before writing to the peer DB, but the outbound neighbor-of-neighbor handshake completion path (`neighbor_handshakes_try_finish` → `handle_neighbor_handshake_accept` → `add_or_schedule_replace_neighbor`) does not perform the same check, allowing a crawled peer to substitute an arbitrary public key identity into the frontier.

### Finding Description
The broken invariant is: `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key) == naddr.public_key_hash` for the `NeighborAddress` originally advertised via `Neighbors` gossip from `cur_neighbor`.

`check_handshake_pubkey_hash` is defined at stackslib/src/net/neighbors/walk.rs:1664-1680 and is explicitly called in `pingback_handshakes_try_finish` (walk.rs:1713-1716) prior to `add_or_schedule_replace_neighbor` (walk.rs:1718). This confirms the check exists specifically to prevent a remote handshake responder from claiming an identity (`node_public_key`) that differs from the pubkey hash the walker expected for that `NeighborAddress`.

The neighbor-of-neighbor discovery flow begins at `getneighbors_try_finish`/`neighbor_handshakes_begin` (walk.rs:979 onward), where addresses harvested from `cur_neighbor`'s `Neighbors` gossip (`neighbor_addrs_to_resolve`, each carrying a `public_key_hash`) are handshaked with individually via `comms.neighbor_session_begin`. The corresponding completion routine for these sessions is `neighbor_handshakes_try_finish`, which processes `HandshakeAccept`/`StackerDBHandshakeAccept` replies and forwards them to `handle_neighbor_handshake_accept`, which in turn calls `add_or_schedule_replace_neighbor` to persist the neighbor into `new_frontier`/PeerDB. Unlike the pingback path, this completion path does not call `check_handshake_pubkey_hash` before persisting the neighbor's claimed `node_public_key`.

### Impact Explanation
An attacker acting as `cur_neighbor` (or one of the neighbor-of-neighbor peers it gossips about) can respond to the walker's handshake probe with a `HandshakeAcceptData` containing a `node_public_key` different from the `public_key_hash` originally gossiped for that `NeighborAddress`. Because the outbound completion path skips `check_handshake_pubkey_hash`, `add_or_schedule_replace_neighbor` → `Neighbor::load_and_update` writes this mismatched identity into `new_frontier` and the PeerDB. This is an unauthenticated write of forged peer identity data into the walker's frontier/state, matching the Critical category ("unauthenticated/unauthorized write to state").

### Likelihood Explanation
The only precondition is that the attacker is (or controls) `cur_neighbor` being walked, or a neighbor-of-neighbor address being probed during a walk — an unprivileged remote peer role explicitly in scope. No secrets, admin roles, or special configuration are required; the attacker simply replies to the walker's outbound `Handshake` with a crafted `HandshakeAccept`. This is repeatable on every walk cycle against the same or different addresses.

### Recommendation
Call `Self::check_handshake_pubkey_hash(&nk, data, &na)` inside `neighbor_handshakes_try_finish`/`handle_neighbor_handshake_accept`, mirroring the check already performed in `pingback_handshakes_try_finish`, before invoking `add_or_schedule_replace_neighbor`, and skip/reject the reply on mismatch.

### Proof of Concept
Add a Rust test in `stackslib::net::neighbors::walk` test module that:
1. Constructs a `NeighborWalk` in state `GetHandshakesFinish` with a `pending_neighbor_addrs`/expected `NeighborAddress` whose `public_key_hash` is `H1`.
2. Injects a mocked reply via `self.comms` containing `StacksMessageType::HandshakeAccept(data)` where `data.handshake.node_public_key` hashes to `H2 != H1`.
3. Calls `neighbor_handshakes_try_finish`.
4. Asserts that `self.new_frontier` (or the PeerDB via `neighbor_db`) now contains an entry keyed/identified by `H2`, proving the mismatched identity was accepted without the `check_handshake_pubkey_hash` gate that exists on the pingback path. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L979-1069)
```rust
    pub fn neighbor_handshakes_begin(
        &mut self,
        network: &mut PeerNetwork,
    ) -> Result<bool, net_error> {
        assert!(self.state == NeighborWalkState::GetHandshakesBegin);

        let my_pubkey_hash = Hash160::from_node_public_key(&Secp256k1PublicKey::from_private(
            &network.get_local_peer().private_key,
        ));

        let pending_neighbor_addrs = self
            .pending_neighbor_addrs
            .take()
            .expect("FATAL: no result from GetNeighbors");

        // got neighbors -- proceed to ask each one for *its* neighbors so we can
        // estimate cur_neighbor's in-degree and grow our frontier.
        debug!(
            "{:?}: will try to connect to {} neighbors of {:?}",
            network.get_local_peer(),
            pending_neighbor_addrs.len(),
            &self.cur_neighbor.addr
        );

        let mut still_pending = vec![];
        for na in pending_neighbor_addrs.into_iter() {
            // don't talk to myself if we're listed as a neighbor of this
            // remote peer.
            if na.public_key_hash == my_pubkey_hash {
                test_debug!(
                    "{:?}: skip handshaking with myself",
                    network.get_local_peer()
                );
                continue;
            }

            // don't handshake with cur_neighbor if we already know its public IP
            // address (we may not know this if the neighbor is inbound)
            if na.addrbytes == self.cur_neighbor.addr.addrbytes
                && na.port == self.cur_neighbor.addr.port
            {
                test_debug!(
                    "{:?}: skip handshaking with cur_neighbor {:?}",
                    network.get_local_peer(),
                    &self.cur_neighbor.addr
                );
                continue;
            }

            let nk = na.to_neighbor_key(network);

            // don't talk to a neighbor if it's unroutable anyway
            if network.is_bound(&nk) || nk.addrbytes.is_anynet() {
                test_debug!(
                    "{:?}: will not connect to bind / anynet address {:?}",
                    network.get_local_peer(),
                    &nk
                );
                continue;
            }

            // start a session with this neighbor
            match self.comms.neighbor_session_begin(network, &na) {
                Ok(true) => {
                    debug!(
                        "{:?}: will Handshake with neighbor-of-neighbor {:?} ({})",
                        network.get_local_peer(),
                        &nk,
                        &na.public_key_hash
                    );
                }
                Ok(false) => {
                    test_debug!(
                        "{:?}: already connecting to {:?}",
                        network.get_local_peer(),
                        &nk
                    );
                    still_pending.push(na);
                    continue;
                }
                Err(e) => {
                    debug!(
                        "{:?}: Failed to connect to {:?}: {:?}",
                        network.get_local_peer(),
                        &nk,
                        &e
                    );
                    continue;
                }
            }
        }
```

**File:** stackslib/src/net/neighbors/walk.rs (L1664-1680)
```rust
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

**File:** stackslib/src/net/neighbors/walk.rs (L1713-1724)
```rust
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
