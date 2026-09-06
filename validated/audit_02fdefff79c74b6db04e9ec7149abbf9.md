Based on direct code inspection, this finding is valid.

### Title
Missing public-key-hash verification in `handle_handshake_accept` allows identity substitution during outbound neighbor walk - (stackslib/src/net/neighbors/walk.rs)

### Summary
`NeighborWalk::handshake_try_finish` calls `handle_handshake_accept` to process a `HandshakeAccept`/`StackerDBHandshakeAccept` reply from the peer dialed as `cur_neighbor`, but this function only checks that the responder's advertised `addr` matches `cur_neighbor.addr` — it never verifies that `data.handshake.node_public_key` hashes to the `public_key_hash` originally associated with the target `NeighborAddress`. As a result, any peer answering at the dialed address can substitute an arbitrary self-declared public key into `self.cur_neighbor` and the PeerDB record persisted via `update_neighbor`.

### Finding Description
In `handle_handshake_accept` (stackslib/src/net/neighbors/walk.rs, lines 676-737), the only identity-binding check performed is: [1](#0-0) 
This compares `neighbor_from_handshake.addr` (built from the handshake payload) against `self.cur_neighbor.addr` — a network-address equality check, not a public-key/hash equality check. There is no call anywhere in this function, nor in `handshake_try_finish` (lines 742-814), to any routine that recomputes `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key)` and compares it against the `public_key_hash` carried by the `NeighborAddress` that was originally dialed (which is how walk targets, including gossip-derived ones from `neighbor_handshakes_begin`/`getneighbors_try_finish`, are represented).

Instead, the function proceeds directly to: [2](#0-1) 
`update_neighbor` is called with `data` (containing the peer's self-declared `node_public_key`) and the result is written back into `self.cur_neighbor`, into `self.new_frontier`, and — via `update_neighbor`'s underlying DB write — into the persistent PeerDB neighbor table, keyed by network address (`NeighborKey`) rather than by public key. Since `NeighborKey` does not include the public key, an address match is sufficient for the walk to accept and persist whatever identity the responder claims, regardless of whether it matches the hash previously advertised for that address by a third party's gossip (`NeighborAddress.public_key_hash`) or by the node's own prior records.

### Impact Explanation
An attacker who controls (or races to answer on) the address associated with a gossiped/known `NeighborAddress` can have their own self-declared public key committed into the node's local `cur_neighbor`/frontier/PeerDB state without any check against the hash that justified selecting that address for the walk. This is an unauthenticated write of attacker-chosen identity data into the node's neighbor state, and it is repeatable on each successful handshake in the outbound walk path.

### Likelihood Explanation
Preconditions: the node must be performing an outbound neighbor walk to `naddr` (an address that carries an expected `public_key_hash`, e.g. one it inherited from gossip via `GetNeighbors`/`Neighbors`), and the attacker must be reachable (or otherwise able to answer) at the address in question and can supply a well-formed `HandshakeAccept` with `node_public_key` set to a key of their own choosing (any key they hold, since only the address is checked). The attacker does not need any secret, node role, or prior trust; running an ordinary peer that responds to dialed handshakes on a claimed address is enough.

### Recommendation
In `handle_handshake_accept`, before calling `update_neighbor`, verify `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key)` against the `public_key_hash` associated with the `NeighborAddress`/`cur_neighbor` that the walk dialed (mirroring the `check_handshake_pubkey_hash` logic used in `pingback_handshakes_try_finish`), and reject/`Err` the handshake (treating it like the existing address-mismatch branch) if the hashes disagree.

### Proof of Concept
Add a Rust unit test in `stackslib/src/net/neighbors/walk.rs`'s test module that: (1) constructs a `NeighborWalk` with `cur_neighbor` set to a `Neighbor` whose `addr.public_key_hash`/expected public key is `pk_A`; (2) drives the walk to `HandshakeFinish`; (3) injects a `StacksMessageType::HandshakeAccept`/`StackerDBHandshakeAccept` reply via the mock `NeighborComms` where `data.handshake.node_public_key` is `pk_B` (different from `pk_A`) but `data.handshake.addrbytes`/`port` match `cur_neighbor.addr`; (4) call `handshake_try_finish`; (5) assert that it returns `Ok(true)` and that `self.cur_neighbor.public_key == pk_B` (or that the mocked `neighbor_db.update_neighbor` was invoked with `pk_B`), demonstrating the forged identity was committed instead of the call returning an error for the pubkey-hash mismatch.

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L705-715)
```rust
        if self.walk_outbound && neighbor_from_handshake.addr != self.cur_neighbor.addr {
            // somehow, got a handshake from someone that _isn't_ cur_neighbor.
            // Note that this does not matter for inbound walks, because we don't always know the
            // real address anyway (since an inbound neighbor might be NAT'ed from us).
            debug!("{}: got unsolicited (or bootstrapping) HandshakeAccept from outbound {:?} (expected {:?})", 
                       local_peer_str,
                       &neighbor_from_handshake.addr,
                       &self.cur_neighbor.addr);

            return Err(net_error::PeerNotConnected(format!("Got unsolicited (or bootstrapping) HandshakeAccept from outbound {:?} (expected {:?})", &neighbor_from_handshake.addr, &self.cur_neighbor.addr)));
        };
```

**File:** stackslib/src/net/neighbors/walk.rs (L722-737)
```rust
        // update our view of `cur_neighbor`, but only if `cur_neighbor` is routable for us.
        // That is not guaranteed to be the case in one instance: this is an inbound walk, and
        // `cur_neighbor` is the very first neighbor we're querying.
        if self.walk_outbound || self.first_neighbor.addr != self.cur_neighbor.addr {
            let cur_neighbor = self.cur_neighbor.clone();
            let new_cur_neighbor =
                self.neighbor_db
                    .update_neighbor(network, cur_neighbor, Some(data), db_data)?;
            self.cur_neighbor = new_cur_neighbor;
        }
        self.new_frontier
            .insert(self.cur_neighbor.addr.clone(), self.cur_neighbor.clone());
        self.neighbor_from_handshake = neighbor_from_handshake.addr;

        Ok(self.cur_neighbor.clone())
    }
```
