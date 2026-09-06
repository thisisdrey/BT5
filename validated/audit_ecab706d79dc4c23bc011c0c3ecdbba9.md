### Title
Unauthenticated identity substitution: `handle_neighbor_handshake_accept`/`add_or_schedule_replace_neighbor` never verify `NeighborAddress.public_key_hash` against the returned handshake public key - ([File: stackslib/src/net/neighbors/walk.rs])

### Summary
`NeighborWalk::check_handshake_pubkey_hash` (walk.rs:1663-1680) is the only place that verifies that a `HandshakeAccept`'s `node_public_key` hashes to the `public_key_hash` originally advertised in a `NeighborAddress`, and it is called exclusively from `pingback_handshakes_try_finish` (walk.rs:1683-1742). The normal outbound "neighbor-of-neighbor" handshake path, `neighbor_handshakes_try_finish` → `handle_neighbor_handshake_accept` (walk.rs:1092-1124) → `NeighborWalkDB::add_or_schedule_replace_neighbor` (db.rs:368-426), stores/updates the `Neighbor` derived purely from the handshake's own `HandshakeData.node_public_key`/address, with no comparison to the `naddr.public_key_hash` that was gossiped by the third-party `cur_neighbor` via `Neighbors`.

### Finding Description
The walk obtains candidate peer addresses (`NeighborAddress`, including `public_key_hash`) from a `Neighbors` reply sent by `cur_neighbor` (`getneighbors_try_finish`, walk.rs:868-927). It then opens sessions to each `NeighborAddress` and sends a `Handshake` (`neighbor_handshakes_begin`, walk.rs:979-1089). When a `HandshakeAccept` comes back, `neighbor_handshakes_try_finish` (walk.rs:1133-1231) calls `handle_neighbor_handshake_accept` with the original `naddr` in scope [1](#0-0) , but that function only calls `add_or_schedule_replace_neighbor(network, preamble, &data.handshake, db_data, ...)` and never uses `naddr.public_key_hash` for any comparison [2](#0-1) . `add_or_schedule_replace_neighbor` in turn builds/saves the neighbor purely from `Neighbor::load_and_update(..., handshake)`, keyed by whatever address/public key the remote peer actually presents in its handshake, with no hash check at all [3](#0-2) .

By contrast, the pingback path explicitly re-derives `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key)` and compares it to `naddr.public_key_hash`, dropping the response on mismatch [4](#0-3) [5](#0-4) . `handle_handshake_accept`, used for the direct `cur_neighbor` handshake (walk.rs:676-737), likewise performs only an address equality check (`neighbor_from_handshake.addr != self.cur_neighbor.addr`, walk.rs:705) and never checks any expected public-key hash — but note that for the *initial* `cur_neighbor` in an outbound walk step, there generally is no separately-advertised `public_key_hash` expectation being enforced beyond the address itself; the walker is willing to accept whatever key the peer at that address presents on first handshake (this is inherent to TOFU-style discovery, and is separately mitigated by requiring a full handshake/signature over the preamble, not a bare identity claim).

The materially exploitable gap is in the **neighbor-of-neighbor path**: an attacker who is `cur_neighbor` (or who controls the network path/response for one of the addresses `cur_neighbor` gossiped) can return a `HandshakeAccept`/`StackerDBHandshakeAccept` whose `handshake.node_public_key` differs from the `public_key_hash` in the original `NeighborAddress` gossiped for that IP:port, and it will still be accepted, stored in `self.new_frontier`, and persisted to `PeerDB` via `add_or_schedule_replace_neighbor`/`neighbor.save()`/`save_update()`. This lets a node inject an attacker-chosen key bound to a third party's address into the walker's frontier and `PeerDB`.

### Impact Explanation
This allows an unprivileged remote peer acting as `cur_neighbor` to plant an unauthenticated, attacker-controlled `(address, public_key)` binding into the victim's `PeerDB`/frontier for the neighbor-of-neighbor discovery flow, since `add_or_schedule_replace_neighbor` performs no `public_key_hash` verification against the gossiped `NeighborAddress` (db.rs:368-426). Because the handshake itself is validly signed by *some* key (the attacker's own), and `Neighbor::load_and_update` keys storage off the handshake's self-reported address/key rather than the previously-advertised hash, this is a real forged/unauthenticated write into peer state — matching the "unauthenticated/unauthorized write to state" Critical category, though the immediate practical blast radius is limited to peer-frontier/PeerDB poisoning (used for future dialing decisions), not direct chain-state corruption.

### Likelihood Explanation
Any remote peer that a victim node walks to as `cur_neighbor` can trigger this: it need only respond with a legitimate `Handshake` (any signature over its own key, no secrets required) that claims one of the addresses it earlier gossiped via `Neighbors`. This is reachable via ordinary P2P walk behavior with no privileged role, secret, or elevated peer status. It is repeatable on every walk cycle.

### Recommendation
In `handle_neighbor_handshake_accept` (walk.rs:1092-1124), before calling `add_or_schedule_replace_neighbor`, verify `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key) == naddr.public_key_hash` (reusing `NeighborWalk::check_handshake_pubkey_hash`), and drop/ignore the response on mismatch, consistent with the pingback path's handling.

### Proof of Concept
Add a test in `stackslib/src/net/neighbors/walk.rs` (or its test module) that: (1) constructs a `NeighborAddress` with a known `public_key_hash` H1 as if gossiped by `cur_neighbor`; (2) crafts a `HandshakeAcceptData` whose `handshake.node_public_key` hashes to a different value H2 (an attacker-controlled keypair); (3) invokes `handle_neighbor_handshake_accept` (or drives `neighbor_handshakes_try_finish` through `PeerNetworkComms`/mock replies) with this mismatched pair; (4) asserts that `self.new_frontier` / `PeerDB::get_peer` nonetheless contains a `Neighbor` entry for that address using the attacker's key H2, and that `NeighborWalk::check_handshake_pubkey_hash(&nk, &data, &naddr)` would have returned `false` for the same inputs, proving the check is bypassed on this path.

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L1104-1122)
```rust
        let (new, neighbor) = self.neighbor_db.add_or_schedule_replace_neighbor(
            network,
            preamble,
            &data.handshake,
            db_data,
            &mut self.neighbor_replacements,
        )?;

        if new {
            // neighbor was new
            self.new_frontier
                .insert(neighbor.addr.clone(), neighbor.clone());
        } else {
            // frontier maintenance
            self.frontier
                .insert(neighbor.addr.clone(), neighbor.clone());
        }

        self.resolved_handshake_neighbors.insert(naddr, neighbor);
```

**File:** stackslib/src/net/neighbors/walk.rs (L1224-1230)
```rust
            self.handle_neighbor_handshake_accept(
                network,
                naddr,
                &message.preamble,
                data,
                db_data,
            )?;
```

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

**File:** stackslib/src/net/neighbors/walk.rs (L1713-1716)
```rust
            let peer_nk = message.to_neighbor_key(&data.handshake.addrbytes, data.handshake.port);
            if !Self::check_handshake_pubkey_hash(&peer_nk, data, &naddr) {
                continue;
            }
```

**File:** stackslib/src/net/neighbors/db.rs (L368-426)
```rust
    fn add_or_schedule_replace_neighbor(
        &self,
        network: &mut PeerNetwork,
        preamble: &Preamble,
        handshake: &HandshakeData,
        db_data: Option<&StackerDBHandshakeData>,
        replacements: &mut NeighborReplacements,
    ) -> Result<(bool, Neighbor), net_error> {
        let local_peer_str = format!("{:?}", network.get_local_peer());
        let tx = network.peerdb_tx_begin()?;
        let (mut neighbor_from_handshake, was_present) =
            Neighbor::load_and_update(&tx, preamble.peer_version, preamble.network_id, handshake)?;

        if was_present {
            test_debug!(
                "{}: already know about neighbor {:?}",
                &local_peer_str,
                &neighbor_from_handshake.addr
            );
            neighbor_from_handshake
                .save_update(&tx, db_data.map(|x| x.smart_contracts.as_slice()))?;
            tx.commit()?;

            // seen this neighbor before
            return Ok((false, neighbor_from_handshake));
        }

        debug!(
            "{}: new neighbor {:?}",
            &local_peer_str, &neighbor_from_handshake.addr
        );

        // didn't know about this neighbor yet. Try to add it.
        let added =
            neighbor_from_handshake.save(&tx, db_data.map(|x| x.smart_contracts.as_slice()))?;

        if added {
            // neighbor was new, and we had space to add it.
            tx.commit()?;
            return Ok((true, neighbor_from_handshake));
        }

        // neighbor was new, but we don't have space to insert it.
        // find and record a neighbor it would replace.
        let replaced_neighbor_slot_opt =
            Self::find_replaced_neighbor_slot(&tx, &neighbor_from_handshake.addr)?;
        if let Some(slot) = replaced_neighbor_slot_opt {
            replacements.add_neighbor(
                NeighborAddress::from_neighbor(&neighbor_from_handshake),
                neighbor_from_handshake.clone(),
                slot,
            );
        }

        tx.commit()?;

        // neighbor was new
        Ok((true, neighbor_from_handshake))
    }
```
