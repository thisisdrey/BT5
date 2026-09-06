### Title
Neighbor-of-neighbor `HandshakeAccept` is trusted for identity/address without validating it against the solicited `NeighborAddress` - ([File: stackslib/src/net/neighbors/walk.rs])

### Summary
In the `GetHandshakesFinish` phase of the neighbor walk, `NeighborWalk::handle_neighbor_handshake_accept` (invoked from `neighbor_handshakes_try_finish`) passes the reply's self-reported `data.handshake` directly to `add_or_schedule_replace_neighbor` without ever checking that `data.handshake.addrbytes`/`port` (or its public-key hash) matches the `naddr: NeighborAddress` that the corresponding `Handshake` request was actually sent to. This is inconsistent with two sibling code paths in the same file — `handle_handshake_accept` (checks `neighbor_from_handshake.addr != self.cur_neighbor.addr` for outbound walks) and `pingback_handshakes_try_finish` (calls `check_handshake_pubkey_hash` against `naddr.public_key_hash`) — both of which perform exactly this validation before touching the PeerDB.

### Finding Description
The claimed equality that should hold is: *the identity (address/pubkey) embedded in a `HandshakeAcceptData` must equal the `NeighborAddress` (`naddr`) that the originating `Handshake` request was sent to.* This equality is enforced in `handle_handshake_accept` (walk.rs lines 705-715) and in `pingback_handshakes_try_finish` via `check_handshake_pubkey_hash` (walk.rs lines 1663-1680, 1713-1716), but it is **not** enforced in `handle_neighbor_handshake_accept`: [1](#0-0) 

Here, `naddr` (the address dialed via `comms.neighbor_session_begin(network, &na)` in `neighbor_handshakes_begin`, walk.rs lines 1004-1048) is only used afterwards for bookkeeping (`self.resolved_handshake_neighbors.insert(naddr, neighbor)`), while the actual identity fed into `add_or_schedule_replace_neighbor` is `&data.handshake`, i.e., whatever address/pubkey the remote peer chose to embed in the payload. `add_or_schedule_replace_neighbor` calls `Neighbor::load_and_update(&tx, preamble.peer_version, preamble.network_id, handshake)` (db.rs lines 368-379), which loads/creates/updates a PeerDB row keyed on the handshake's own `addrbytes`/`port` — not on `naddr`.

The `Preamble.sign()`/`verify()` mechanism (codec.rs lines 77-140) only proves that the message was signed by the private key matching `data.handshake.node_public_key`; it proves nothing about whether that key's owner is actually reachable at `data.handshake.addrbytes:port`. So a peer X, dialed at `naddr` (X's real address), can reply to the Handshake request with a self-signed `HandshakeAccept` whose `data.handshake.addrbytes/port` claims to be an arbitrary address Y that the walking node never contacted. `handle_neighbor_handshake_accept` will accept this and call `add_or_schedule_replace_neighbor`, writing/growing a PeerDB entry for Y based solely on X's unverified claim, and will add it to `self.new_frontier` / `self.frontier`, from which it later gets gossiped onward as a `NeighborAddress` in `GetNeighbors` responses to other peers (walk.rs lines 1239-1257).

### Impact Explanation
This allows an unprivileged remote peer X to inject forged/unauthenticated neighbor entries into another node's PeerDB frontier and propagate them further to other crawling peers via subsequent `Neighbors` responses, without ever needing control of Y's address or key ownership by Y at that address. This is a network-wide propagation of forged peer/frontier data, matching the "network-wide propagation of forged data" Critical category — a compromised or malicious peer can poison the address book of honest nodes and steer future connection/discovery targets toward addresses it wants to bait (e.g., addresses it controls under a different guise, or addresses it wants to cause the network to load-generate traffic toward). Repeatable per neighbor-of-neighbor handshake round, at negligible attacker cost.

### Likelihood Explanation
Preconditions: attacker only needs to run an ordinary peer that another node discovers via `GetNeighbors` and dials for a neighbor-of-neighbor handshake (`GetHandshakesBegin`/`GetHandshakesFinish` state), which is normal, unprivileged crawling behavior in the P2P network. No RPC secret, no signer role, no privileged key ownership of Y is required — the attacker only needs its own valid keypair to sign the (self-consistent, protocol-valid) `HandshakeAccept`. This is fully remotely reachable over the P2P port and repeatable on every walk round.

### Recommendation
In `handle_neighbor_handshake_accept`, before calling `add_or_schedule_replace_neighbor`, validate the reply against `naddr` the same way `pingback_handshakes_try_finish` does with `check_handshake_pubkey_hash` (and/or compare `data.handshake.addrbytes`/`port` against `naddr.to_neighbor_key(network)`), rejecting/dropping the reply (and marking the connection broken) on mismatch, consistent with the check already present in `handle_handshake_accept`.

### Proof of Concept
Rust test plan (in `stackslib/src/net/neighbors/walk.rs` test module, mirroring existing walk tests):
1. Set up two `TestPeer`s: local node L and neighbor X, with X listed as a resolvable `NeighborAddress` (`naddr`) in L's `pending_neighbor_addrs` for `cur_neighbor`.
2. Drive `neighbor_handshakes_begin` so L opens a session to `naddr` (X's real address/port) and sends `Handshake`.
3. Craft X's reply as a `StacksMessageType::HandshakeAccept(HandshakeAcceptData { handshake: HandshakeData { addrbytes: Y_ADDR, port: Y_PORT, node_public_key: Y_OR_X_KEY, .. }, .. })`, signed with X's private key (self-consistent signature, X only needs its own key), and feed it into L's comms as the reply keyed to `naddr`.
4. Call `neighbor_handshakes_try_finish`.
5. Assert: `network.peerdb_conn()` now contains a `PeerDB` row for `(Y_ADDR, Y_PORT)` (e.g., via `PeerDB::get_peer`), and `walk.new_frontier`/`walk.frontier` contains an entry keyed on Y's address — despite L never having dialed or independently verified Y at `Y_ADDR:Y_PORT`. This demonstrates `add_or_schedule_replace_neighbor` inserted Y based purely on X's unverified, self-signed claim, with no equality check against `naddr`.

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L1092-1124)
```rust
    fn handle_neighbor_handshake_accept(
        &mut self,
        network: &mut PeerNetwork,
        naddr: NeighborAddress,
        preamble: &Preamble,
        data: &HandshakeAcceptData,
        db_data: Option<&StackerDBHandshakeData>,
    ) -> Result<(), net_error> {
        // NOTE: even if cur_neighbor is an inbound neighbor, the neighbors
        // of cur_neighbor that we could handshake with are necessarily
        // outbound connections.  So, save them all.
        // Do we know about this peer already?
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
        Ok(())
    }
```
