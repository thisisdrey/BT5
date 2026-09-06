Confirmed: in `neighbor_handshakes_try_finish` (stackslib/src/net/neighbors/walk.rs), the reply's `naddr` (the neighbor address advertised by `cur_neighbor` — analogous to the expected "hostname") is passed straight into `handle_neighbor_handshake_accept` without ever checking that the `HandshakeAcceptData`'s `node_public_key` hashes to `naddr.public_key_hash`. This is the exact same equality that `pingback_handshakes_try_finish` (lines 1683-1725) *does* enforce via `check_handshake_pubkey_hash(&peer_nk, data, &naddr)` before calling `add_or_schedule_replace_neighbor`. In the neighbor-of-neighbor handshake path, `handle_neighbor_handshake_accept` calls `add_or_schedule_replace_neighbor` directly, keyed only on `(addrbytes, port)` from the handshake, and stores/updates the peer's public key without ever comparing it to the `naddr.public_key_hash` that was gossiped by `cur_neighbor` and used to select who to contact.

### Title
Missing Public-Key-Hash Verification in Neighbor-of-Neighbor Handshake Processing Allows Peer Identity Spoofing - (File: stackslib/src/net/neighbors/walk.rs)

### Summary
`NeighborWalk::neighbor_handshakes_try_finish` accepts `HandshakeAccept`/`StackerDBHandshakeAccept` replies from neighbors-of-neighbors and forwards them to `handle_neighbor_handshake_accept` without ever validating that the public key in the handshake matches the `public_key_hash` advertised for that `NeighborAddress`. This omission mirrors the CVE-2014-3604 bug class (accepting an otherwise-valid credential without checking it belongs to the claimed identity).

### Finding Description
When a node walks the neighbor graph, `getneighbors_try_finish` collects `NeighborAddress` entries (each carrying an expected `public_key_hash`) reported by `cur_neighbor`, and the walk proceeds to handshake with each of those addresses [1](#0-0) . When replies come back in `neighbor_handshakes_try_finish`, each is dispatched to `handle_neighbor_handshake_accept(network, naddr, &message.preamble, data, db_data)` [2](#0-1) .

`handle_neighbor_handshake_accept` immediately calls `self.neighbor_db.add_or_schedule_replace_neighbor(network, preamble, &data.handshake, db_data, ...)`, which loads/creates a `Neighbor` keyed by `(network_id, addrbytes, port)` and stores the public key taken from the handshake — with no comparison against `naddr.public_key_hash` at all [3](#0-2) . `add_or_schedule_replace_neighbor` and the underlying `Neighbor::load_and_update` key exclusively on IP/port, and unconditionally overwrite the stored public key with whatever key came back in this handshake [4](#0-3) [5](#0-4) .

This is in stark contrast to the sibling code path, `pingback_handshakes_try_finish`, which explicitly performs `Self::check_handshake_pubkey_hash(&peer_nk, data, &naddr)` and skips the neighbor entirely if the hash doesn't match before ever calling `add_or_schedule_replace_neighbor` [6](#0-5) . The helper `check_handshake_pubkey_hash` exists precisely to enforce this equality (`Hash160::from_node_public_key_buffer(&data.handshake.node_public_key) == naddr.public_key_hash`) [7](#0-6) , but it is never invoked from `handle_neighbor_handshake_accept`/`neighbor_handshakes_try_finish`.

The broken equality is: "the public key hash claimed by a gossiped `NeighborAddress` (the 'hostname')" vs "the public key actually returned in the handshake reply (the 'certificate')". Just as Not Yet Commons SSL failed to check the presented certificate's CN against the requested hostname, this code fails to check the presented handshake's public key against the requested `NeighborAddress.public_key_hash`.

### Impact Explanation
A malicious/compromised `cur_neighbor` can gossip a set of `NeighborAddress` records containing arbitrary `(addrbytes, port, public_key_hash)` triples pointing at IP:port combinations it controls (e.g., itself under multiple listeners, or third-party IP/ports it can respond on/intercept). Any Stacks node's `PeerNetwork` will attempt to handshake with those addresses; when the attacker's node replies at any of those addresses with a *different, attacker-controlled* key than what was advertised, `handle_neighbor_handshake_accept` will store that (addr,port) → attacker key mapping into the local `PeerDB` without complaint, resulting in the network's frontier/peer table recording spoofed peer identities. Because Neighbor identity in this p2p layer underpins peer authentication and relay-target selection (`Neighbor::load_by_address` legitimately relies on this hash matching to decide freshness), poisoning it can facilitate impersonation of specific peers, misdirected relay/broadcast traffic, and peer-table pollution across a walking node's peer graph. This corresponds to the High-impact category "steering a node off the tip via false inventory" / unauthorized write to local peer state via a spoofed identity accepted as canonical.

### Likelihood Explanation
This is trivially reachable by any remote, unprivileged peer that a victim node ever selects as `cur_neighbor` during a routine neighbor walk (an entirely normal, automatic, and frequent p2p operation) — the attacker only needs to run a single Stacks node and craft `Neighbors` and `HandshakeAccept` responses; no signature-forgery or protocol violation is required, since the attacker's handshake reply is self-consistently and correctly signed by their own key, it's simply never checked against the address it was solicited under.

### Recommendation
In `handle_neighbor_handshake_accept` (or immediately before it is invoked, in `neighbor_handshakes_try_finish`), call `Self::check_handshake_pubkey_hash(&nkey, data, &naddr)` (the same check already applied in `pingback_handshakes_try_finish`) and skip/reject the neighbor (treat like an out-of-sequence/broken reply) if the reported public key does not hash to `naddr.public_key_hash`, before calling `add_or_schedule_replace_neighbor`.

### Proof of Concept
1. Attacker runs a Stacks node `A` and gets selected as `cur_neighbor` by victim `V` during a neighbor walk.
2. When `V` sends `GetNeighbors` to `A`, `A` replies with a `Neighbors` message containing a `NeighborAddress { addrbytes: X, port: P, public_key_hash: H_fake }` for some IP:port `X:P` that `A` also controls (or a co-located victim service reachable at `X:P`).
3. `V`'s `NeighborWalk` proceeds to `getneighbors_try_finish`, adds `X:P` to `neighbor_addrs_to_resolve`, and sends it a `Handshake`.
4. `A`, listening on `X:P`, replies with `HandshakeAccept` signed by a different key `K_real` (not matching `H_fake`).
5. In `neighbor_handshakes_try_finish`, `V` calls `handle_neighbor_handshake_accept(network, naddr, preamble, data, db_data)` with `naddr.public_key_hash == H_fake` but `data.handshake.node_public_key` hashing to `K_real` — no check compares these, so `add_or_schedule_replace_neighbor` stores/updates a `Neighbor` at `X:P` with public key `K_real`, silently overwriting whatever was expected/known for that identity, contrary to `pingback_handshakes_try_finish`'s enforced check at [8](#0-7) .

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L929-954)
```rust
        // prune the list to a reasonable size in case cur_neighbor gave us too many for our
        // configuration
        if neighbor_addrs_to_resolve.len() as u64
            > network.get_connection_opts().max_neighbors_of_neighbor
        {
            debug!(
                "{:?}: will handshake with {} neighbors out of {} reported by {:?}",
                network.get_local_peer(),
                network.get_connection_opts().max_neighbors_of_neighbor,
                neighbor_addrs_to_resolve.len(),
                &self.cur_neighbor.addr
            );
            neighbor_addrs_to_resolve.shuffle(&mut thread_rng());
            neighbor_addrs_to_resolve
                .truncate(network.get_connection_opts().max_neighbors_of_neighbor as usize);
        }

        // proceed to handshake with them.
        // also, try to handshake with the current neighbor's advertized IP address (it might be
        // different than the one we use)
        test_debug!("{:?}: will try to handshake with inbound neighbor {:?}'s advertized address {:?} as well", network.get_local_peer(), &self.cur_neighbor.addr, &self.neighbor_from_handshake);
        let cur_neighbor_pubkey_hash = Hash160::from_node_public_key(&self.cur_neighbor.public_key);
        neighbor_addrs_to_resolve.push(NeighborAddress::from_neighbor_key(
            self.neighbor_from_handshake.clone(),
            cur_neighbor_pubkey_hash,
        ));
```

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

**File:** stackslib/src/net/neighbors/walk.rs (L1216-1230)
```rust
            debug!(
                "{:?}: Got HandshakeAccept from {:?}",
                network.get_local_peer(),
                &nkey;
                "handshake_data" => ?data,
                "stackerdb_data" => ?db_data
            );

            self.handle_neighbor_handshake_accept(
                network,
                naddr,
                &message.preamble,
                data,
                db_data,
            )?;
```

**File:** stackslib/src/net/neighbors/walk.rs (L1663-1725)
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

    /// Finish up connecting to newly-discovered inbound peers
    pub fn pingback_handshakes_try_finish(
        &mut self,
        network: &mut PeerNetwork,
    ) -> Result<bool, net_error> {
        assert!(self.state == NeighborWalkState::PingbackHandshakesFinish);

        // see if we got any replies
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

**File:** stackslib/src/net/chat.rs (L467-516)
```rust
    /// Instantiate a Neighbor from HandshakeData, merging the information we have on-disk in the
    /// PeerDB with information in the handshake.
    /// * If we already know about this neighbor, then all previously-calculated state and local
    ///   configuration state will be loaded as well.  This includes things like the calculated
    ///   in/out-degree and last-contact time, as well as the allow/deny time limits.
    /// * If we do not know about this neighbor, then the above state will not be loaded.
    ///
    /// Returns (the neighbor, whether or not the neighbor was known)
    pub fn load_and_update(
        conn: &DBConn,
        peer_version: u32,
        network_id: u32,
        handshake_data: &HandshakeData,
    ) -> Result<(Neighbor, bool), net_error> {
        let addr = NeighborKey::from_handshake(peer_version, network_id, handshake_data);
        let pubk = handshake_data
            .node_public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;

        let peer_opt = PeerDB::get_peer(conn, network_id, &addr.addrbytes, addr.port)
            .map_err(net_error::DBError)?;

        let (mut neighbor, present) = match peer_opt {
            Some(neighbor) => {
                let mut ret = neighbor;
                ret.addr = addr.clone();
                (ret, true)
            }
            None => {
                let ret = Neighbor::empty(&addr, &pubk, handshake_data.expire_block_height);
                (ret, false)
            }
        };

        #[cfg(test)]
        {
            // setting BLOCKSTACK_NEIGHBOR_TEST_${PORTNUMBER} will let us select an organization
            // for this peer
            use std::env;
            if let Ok(asn_str) = env::var(format!("BLOCKSTACK_NEIGHBOR_TEST_{}", addr.port)) {
                neighbor.asn = asn_str.parse().unwrap();
                neighbor.org = neighbor.asn;
                test_debug!("Override {:?} to ASN/org {}", &neighbor.addr, neighbor.asn);
            };
        }

        neighbor.handshake_update(conn, handshake_data)?;
        Ok((neighbor, present))
    }
```
