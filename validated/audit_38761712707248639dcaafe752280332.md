### Title
Neighbor-of-neighbor `HandshakeAccept` stores attacker-claimed address into PeerDB without binding it to the dialed socket address - ([File: stackslib/src/net/neighbors/walk.rs])

### Summary
When a node walks the network and asks a known neighbor for *its* neighbors (`GetNeighbors`), it then dials each reported `NeighborAddress` directly and expects a `HandshakeAccept`. In `handle_neighbor_handshake_accept` (called from `neighbor_handshakes_try_finish`), the self-declared `HandshakeData.addrbytes`/`port` in the reply is trusted and written into `PeerDB` via `add_or_schedule_replace_neighbor` → `Neighbor::load_and_update`/`save` → `PeerDB::insert_or_replace_peer`, with no check that it matches the `naddr` address that was actually dialed. This is the exact address/authentication-binding gap the question describes, but it exists specifically in the neighbor-of-neighbor discovery path rather than the direct inbound `Handshake` path (which is already correctly restricted).

### Finding Description
`ConversationP2P::validate_handshake` (`stackslib/src/net/chat.rs:1047-1092`) only checks that the self-declared handshake address matches the socket address for **outbound** connections when a public key is already known:
```
if self.stats.outbound
    && (!handshake_data.addrbytes.is_anynet()
        && (self.peer_addrbytes != handshake_data.addrbytes
            || self.peer_port != handshake_data.port))
{
    return Err(net_error::InvalidHandshake);
}
``` [1](#0-0) 
For inbound connections, or when the public key is not yet known, this equality is never enforced by design (the comment explicitly says inbound addresses cannot be trusted to match the socket).

Separately, `NeighborWalk::handle_handshake_accept` (`stackslib/src/net/neighbors/walk.rs:669-737`), used when directly walking `cur_neighbor`, *does* enforce that the reported address equals the neighbor actually dialed:
```
if self.walk_outbound && neighbor_from_handshake.addr != self.cur_neighbor.addr {
    return Err(net_error::PeerNotConnected(...));
}
``` [2](#0-1) 

However, when the walker asks `cur_neighbor` for *its* neighbors and then handshakes with each of those neighbor-of-neighbor addresses, the accept is processed by `handle_neighbor_handshake_accept`:
```rust
fn handle_neighbor_handshake_accept(
    &mut self, network: &mut PeerNetwork, naddr: NeighborAddress,
    preamble: &Preamble, data: &HandshakeAcceptData,
    db_data: Option<&StackerDBHandshakeData>,
) -> Result<(), net_error> {
    // NOTE: even if cur_neighbor is an inbound neighbor, the neighbors
    // of cur_neighbor that we could handshake with are necessarily
    // outbound connections.  So, save them all.
    let (new, neighbor) = self.neighbor_db.add_or_schedule_replace_neighbor(
        network, preamble, &data.handshake, db_data, &mut self.neighbor_replacements,
    )?;
    ...
``` [3](#0-2) 
Unlike `pingback_handshakes_try_finish` (which at least calls `check_handshake_pubkey_hash` against the expected `naddr`), this function performs **no comparison at all** between `naddr` (the address actually dialed) and `data.handshake.addrbytes`/`port` (the self-declared address inside the signed `HandshakeData`), and no pubkey-hash check either.

`add_or_schedule_replace_neighbor` (`stackslib/src/net/neighbors/db.rs:368-426`) builds the `Neighbor`/`NeighborKey` purely from `handshake.addrbytes`/`port` via `Neighbor::load_and_update` → `NeighborKey::from_handshake` (`stackslib/src/net/chat.rs:411-423`), then calls `neighbor.save(...)`, which (per `try_insert_peer`, `stackslib/src/net/db.rs:1452-1488`) ultimately calls `PeerDB::insert_or_replace_peer` (`stackslib/src/net/db.rs:1091-1132`) storing exactly that self-declared `addrbytes`/`port` into the `frontier` table.

**Exploit flow:** Attacker runs a legitimate reachable peer at IP A. The victim walks to some neighbor (which can be the attacker's own node, or any peer that includes the attacker's node in a `Neighbors` reply) and asks for neighbors-of-neighbors, receiving a `NeighborAddress` for the attacker at IP A (real, dialable). The victim connects out to A, sends a `Handshake`, and the attacker (validly signing with its own private key) replies with `HandshakeAccept` whose `HandshakeData.addrbytes/port` claims IP B/port (a victim-chosen or arbitrary third-party address the attacker does not control). Signature verification (`message.verify_secp256k1`) only checks that the message is signed by the key named in the payload — it never binds the signature to any particular network address. `handle_neighbor_handshake_accept` accepts this unconditionally and calls `PeerDB::insert_or_replace_peer`, writing `(addrbytes=B, port=B_port, public_key_hash=attacker_pubkey)` into the frontier.

### Impact Explanation
This is an unauthenticated write into the node's `PeerDB` frontier state: an address the attacker does not control (B) gets permanently associated in the victim's local database with the attacker's public key. Consequences:
- Frontier poisoning: the victim's node may subsequently attempt outbound connections/handshakes to B believing it is the attacker's node, and may gossip this (address, pubkey) pair to further peers via `GetNeighbors`/`Neighbors` responses, propagating the forged record to other nodes on the network (network-wide propagation of forged data).
- Repeatable: the attacker can send arbitrarily many distinct fake B addresses across successive walk cycles (bounded only by PeerDB slot capacity), continuously polluting frontier data with no signature over the address field.

This matches the Critical category "unauthenticated/unauthorized write to state ... network-wide propagation of forged data."

### Likelihood Explanation
- Preconditions: attacker just needs to be a reachable P2P peer that the victim's neighbor-walk state machine dials as part of `GetHandshakesFinish`/`neighbor_handshakes_try_finish`. This requires no privileged role, no secret, and no compromise of any existing peer — only running an unprivileged, publicly reachable Stacks P2P node, which any remote party can do.
- Attacker cost: one valid signed `HandshakeAccept` message per forged record; trivially repeatable.
- Reachability: this is exercised automatically by any node performing routine neighbor-walk gossip (a normal periodic background process), so it will be triggered by any victim node without further interaction.

### Recommendation
In `handle_neighbor_handshake_accept` (and any other place that ingests a `HandshakeAcceptData` for a peer that was reached at a known address, e.g. the `naddr` passed in), require that `data.handshake.addrbytes`/`port` matches the `naddr`/dialed socket address (mirroring the check already done in `NeighborWalk::handle_handshake_accept` for `cur_neighbor`), and also validate `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key) == naddr.public_key_hash` (mirroring `check_handshake_pubkey_hash`, already used in `pingback_handshakes_try_finish` but missing here). Reject/drop the reply rather than storing a peer whose declared address diverges from the address actually contacted, unless the declared address is `is_anynet()` (unknown self-IP case), in which case pin the stored address to the observed/dialed address instead of the claimed one.

### Proof of Concept
Rust test plan in `stackslib/src/net/neighbors/walk.rs` (or a new test module reusing `NeighborWalk` test scaffolding used elsewhere, e.g. patterns in `stackslib/src/net/tests/`):
1. Set up two `PeerNetwork`/`PeerDB` instances (victim and attacker), as done in existing chat/db tests (`test_peer_insert_and_retrieval`, `convo_handshake_*` helpers).
2. Configure the victim's `NeighborWalk` to be in `GetHandshakesFinish` state with a `naddr` pointing at the attacker's real bound socket address A.
3. Have the attacker respond to the victim's `Handshake` with a `StacksMessageType::HandshakeAccept` whose `HandshakeData.addrbytes`/`port` is set to an arbitrary address B (e.g., `PeerAddress` for `10.0.0.99:20444`) different from A, signed correctly by the attacker's private key.
4. Call `NeighborWalk::neighbor_handshakes_try_finish` with this crafted reply.
5. Assert: `PeerDB::get_peer(victim_conn, network_id, &addrbytes_B, port_B)` returns `Some(neighbor)` with `neighbor.public_key == attacker_pubkey` — i.e., the frontier stores address B under the attacker's key even though the victim only ever reached the attacker at address A. A fixed implementation should instead return `None` for B (rejected) or store the entry under address A.

### Citations

**File:** stackslib/src/net/chat.rs (L1072-1091)
```rust
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

**File:** stackslib/src/net/neighbors/walk.rs (L1092-1123)
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
```
