### Title
Pingback-handshake verification checks only pubkey hash, not address, allowing frontier-DB address forgery - (File: stackslib/src/net/neighbors/walk.rs)

### Summary
`pingback_handshakes_try_finish` verifies only that the pingback reply's public key hashes to the expected `naddr.public_key_hash`, then stores the neighbor using the *self-reported* `data.handshake.addrbytes`/`port` instead of `naddr`'s address. An attacker who is being verified via pingback can legitimately hold the private key for the verified pubkey hash yet report an arbitrary, unrelated `addrbytes`/`port`, causing the frontier DB to persist a forged (pubkey, address) association.

### Finding Description
In `pingback_handshakes_try_finish`, the only validation performed on the `HandshakeAccept`/`StackerDBHandshakeAccept` reply is `check_handshake_pubkey_hash`, which compares `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key)` to `naddr.public_key_hash` and returns `true`/`false` — it never compares `data.handshake.addrbytes`/`port` to `naddr.addrbytes`/`port`: [1](#0-0) 

Immediately after this pubkey-only check passes, the handshake data (containing the attacker-controlled address fields) is handed directly to `add_or_schedule_replace_neighbor`: [2](#0-1) 

`add_or_schedule_replace_neighbor` calls `Neighbor::load_and_update(..., handshake)`, which derives the stored `NeighborKey`/`Neighbor.addr` from `handshake.addrbytes`/`port` — the value supplied entirely by the remote peer in that message, not `naddr`: [3](#0-2) 

This is in sharp contrast to the normal (non-pingback) neighbor-walk handshake-accept path, `NeighborWalk::handle_handshake_accept`, which explicitly enforces address consistency for outbound walks: `if self.walk_outbound && neighbor_from_handshake.addr != self.cur_neighbor.addr { ... return Err(...) }`. That guard is absent from `pingback_handshakes_try_finish`: [4](#0-3) 

**Exploit flow**: An attacker connects inbound to the victim node and completes a normal, properly signed `Handshake` with public key `K`, self-reporting address `A` (own reachable infrastructure) in the handshake payload. `A` becomes `naddr` in `walk_pingbacks` (`schedule_network_pingbacks`, `stackslib/src/net/p2p.rs:4327-4370`, using `convo.to_handshake_neighbor_address()`). When the victim later dials back to `A` to verify routability, the attacker's listener at `A` completes a fresh handshake session and replies with `HandshakeAccept` where `data.handshake.node_public_key == K` (so `check_handshake_pubkey_hash` passes) but `data.handshake.addrbytes`/`port` are set to an arbitrary value `B` (e.g., a victim third-party's IP:port). The pubkey-hash check succeeds, and the frontier DB persists `Neighbor { addr: B, public_key: K, ... }` — an entry the pingback walk never actually verified as routable, since `B`'s reachability was never contacted at all.

### Impact Explanation
The victim node's PeerDB frontier now contains a forged `(pubkey K, address B)` record that was never independently verified — it is only the attacker's self-report. This record propagates outward: any peer that later queries this node via `GetNeighbors` may receive `B` as an advertised neighbor (subject to `filter_sensible_neighbors`), causing other nodes across the network to add `B` to their own frontiers and attempt outbound connections to it. This is a network-wide propagation of attacker-forged peer data (an unauthenticated, unverified address claim laundered as a "verified" frontier entry), and can be used to direct many nodes' connection attempts at an arbitrary third-party IP:port chosen entirely by the attacker.

### Likelihood Explanation
Preconditions are minimal and fully attacker-controlled: the attacker only needs (1) a normal, properly-signed keypair and (2) a publicly reachable listener at the self-reported address `A` to receive the victim's pingback connection. No secrets, privileged roles, or races with consensus state are required. The pingback-walk path is a routine part of the node's neighbor-discovery/crawling cycle and is triggered automatically for any newly-observed inbound, authenticated peer, making this reliably and repeatedly triggerable per attacker-controlled inbound session.

### Recommendation
In `check_handshake_pubkey_hash` (or immediately after, in `pingback_handshakes_try_finish`), also validate that `data.handshake.addrbytes == naddr.addrbytes && data.handshake.port == naddr.port` before calling `add_or_schedule_replace_neighbor`, mirroring the consistency check already present in `NeighborWalk::handle_handshake_accept` (`stackslib/src/net/neighbors/walk.rs:705-715`). Reject/skip the pingback result (treat as failed pingback) if the address does not match `naddr`.

### Proof of Concept
Add a Rust test in `stackslib::net::neighbors::walk` tests module:
1. Construct a `NeighborWalk` in `PingbackHandshakesFinish` state with a `naddr` (`NeighborAddress { addrbytes: A, port: pA, public_key_hash: H(K) }`) as the pending pingback target, using a mock `NeighborComms` (as done in existing walk tests).
2. Inject a synthetic reply message: `StacksMessageType::HandshakeAccept(HandshakeAcceptData { handshake: HandshakeData { node_public_key: K, addrbytes: B, port: pB, ... }, ... })` where `B != A`, `pB != pA`, but `Hash160::from_node_public_key_buffer(&K) == naddr.public_key_hash`.
3. Call `pingback_handshakes_try_finish(&mut network)`.
4. Assert failure of the expectation: query the resulting stored `Neighbor` (via the mock/real `NeighborWalkDB`) and assert `neighbor.addr == naddr` (i.e., `A`/`pA`) — currently this assertion fails because the stored `neighbor.addr` equals `B`/`pB` instead, demonstrating the address-substitution vulnerability.

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

**File:** stackslib/src/net/neighbors/walk.rs (L1713-1725)
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
        }
```

**File:** stackslib/src/net/neighbors/db.rs (L368-393)
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
```
