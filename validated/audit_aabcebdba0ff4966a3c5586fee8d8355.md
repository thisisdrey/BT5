### Title
Pingback HandshakeAccept stores forged self-reported address into PeerDB without validating it against the actual pingback destination - ([File: stackslib/src/net/neighbors/db.rs])

### Summary
In the pingback-handshake completion path, the only check performed on an inbound `HandshakeAcceptData` is that its embedded public key hashes to the pubkey already associated with the pingback target; the self-reported `addrbytes`/`port` fields inside `handshake` are never compared to the address the pingback probe was actually sent to. `add_or_schedule_replace_neighbor` then persists a `Neighbor` keyed purely off those unverified, attacker-controlled fields, letting a peer that legitimately controls address A register an arbitrary address B in the victim's frontier under its own (correctly verified) public key.

### Finding Description
The equality that should hold is: *the address used to route/verify the HandshakeAccept reply (the pingback destination we actually connected to) == the address stored in the PeerDB record derived from that reply*. This equality is broken.

- `PeerNetwork::schedule_network_pingbacks` (`stackslib/src/net/p2p.rs:4305-4394`) schedules a pingback to `addr = convo.to_handshake_neighbor_address()`, i.e., the self-reported address the inbound peer gave in its *original* handshake. Our node then opens a fresh outbound connection to that address and sends a Handshake.
- `NeighborWalk::pingback_handshakes_try_finish` (`stackslib/src/net/neighbors/walk.rs:1683-1725`) receives the reply and validates it with `check_handshake_pubkey_hash` (`walk.rs:1663-1680`), which only checks `Hash160::from_node_public_key_buffer(&data.handshake.node_public_key) == naddr.public_key_hash`. The `nk` (`peer_nk = message.to_neighbor_key(&data.handshake.addrbytes, data.handshake.port)`, `walk.rs:1713`) built from the handshake's self-reported `addrbytes`/`port` is passed into that function only for a debug log — it is never compared to `naddr` (the actual address we pingback-connected to).
- The accepted `data.handshake` (with attacker-controlled `addrbytes`/`port`, call it address B) is then passed directly to `NeighborWalkDB::add_or_schedule_replace_neighbor` (`stackslib/src/net/neighbors/db.rs:368-379`), which calls `Neighbor::load_and_update(&tx, preamble.peer_version, preamble.network_id, handshake)` (`stackslib/src/net/chat.rs:475-516`). That function builds the storage key via `NeighborKey::from_handshake(peer_version, network_id, handshake_data)` — i.e., purely from the embedded (forged) `addrbytes`/`port` — and `Neighbor::save`/`save_update` writes a PeerDB row at that address (`neighbors/db.rs:387-407`).

An attacker must genuinely control the box that receives our pingback (to produce a validly-signed handshake for the pubkey hash check to pass), but nothing stops them from putting a *different* IP/port (B) in the `HandshakeData.addrbytes`/`port` fields of that same accept message. The result is a PeerDB entry claiming address B is reachable and belongs to the attacker's real, verified public key, even though B never answered anything on the wire.

This differs from the direct-handshake path (`NeighborWalk::handle_handshake_accept`, `walk.rs:669-737`), which explicitly checks `neighbor_from_handshake.addr != self.cur_neighbor.addr` for outbound walks (`walk.rs:705-715`) and rejects mismatches. That guard is absent from the pingback path and from `handle_neighbor_handshake_accept` (`walk.rs:1092-1124`, used by `neighbor_handshakes_try_finish`), both of which call `add_or_schedule_replace_neighbor` with unchecked handshake-embedded addresses.

### Impact Explanation
A single crafted `HandshakeAccept`/`StackerDBHandshakeAccept` reply causes the victim node to write an unauthenticated peer record into its own PeerDB frontier at an address the attacker does not actually occupy on the wire, tagged with the attacker's real (verified) public key. Since PeerDB frontier entries are gossiped to other crawling peers via `GetNeighbors`/handshake responses, this forged mapping can propagate network-wide, causing other nodes to also learn/attempt connections to the spoofed address — an unauthenticated write of state plus network-wide propagation of forged frontier data, matching the Critical category ("unauthenticated write to state" / "network-wide propagation of forged data"). This is repeatable per pingback cycle and requires no privileged role.

### Likelihood Explanation
Precondition: the attacker must run a normal, unprivileged node that connects inbound to the victim, completes a normal handshake (so a pingback gets scheduled), and then answers the outbound pingback probe from a box it controls, but forges the `addrbytes`/`port` fields inside the `HandshakeData` of its `HandshakeAccept` reply. This requires no secret, no special role, and is entirely achievable by any remote peer able to accept inbound connections and speak the P2P protocol. Cost is a normal handshake plus one crafted reply; repeatable at will against any node that performs neighbor-walk pingbacks (a routine, default behavior).

### Recommendation
In `pingback_handshakes_try_finish` (and `handle_neighbor_handshake_accept`), validate that `data.handshake.addrbytes`/`port` match the address actually used to route/dial the reply (`naddr`/the connection's verified peer address) before calling `add_or_schedule_replace_neighbor`, analogous to the `neighbor_from_handshake.addr != self.cur_neighbor.addr` check already done in `handle_handshake_accept` (`walk.rs:705-715`). Reject or fall back to the dialed address (as is already done for private/anynet addresses at `walk.rs:693-703`) rather than trusting the self-reported fields unconditionally.

### Proof of Concept
Rust test plan in `stackslib/src/net/neighbors/walk.rs` (or a new test module alongside existing pingback tests):
1. Set up two mock `PeerNetwork`/`NeighborWalk` instances, `net_a` (victim) and `net_b` (attacker), each with a real `PeerDB`.
2. Have `net_b` connect inbound to `net_a` and complete a normal handshake with self-reported address A (so `net_a` schedules a pingback to A per `schedule_network_pingbacks`).
3. Drive `net_a` into `NeighborWalkState::PingbackHandshakesFinish`, with `net_a` opening an outbound connection to A.
4. Have `net_b`, when replying to the pingback probe on the connection at A, send a `StacksMessageType::StackerDBHandshakeAccept`/`HandshakeAccept` whose `HandshakeData` is validly signed by `net_b`'s real private key but has `addrbytes`/`port` set to an arbitrary address B (e.g., a third, uninvolved IP) instead of A.
5. Call `pingback_handshakes_try_finish` on `net_a` and then query `net_a`'s `PeerDB` via `PeerDB::get_peer(conn, network_id, &B, port_b)`.
6. Assert that a `Neighbor` record now exists at address B with `net_b`'s public key, i.e. `neighbor.public_key == net_b_pubkey && neighbor.addr.addrbytes == B`, while the pingback connection/reply physically arrived from A — demonstrating the stored identity/address does not correspond to the address that answered on the wire. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** stackslib/src/net/neighbors/walk.rs (L669-737)
```rust
    /// Handle a HandshakeAcceptData.
    /// Update the PeerDB information from the handshake data, as well as `self.cur_neighbor`, if
    /// this neighbor was routable.  If it's not routable (i.e. we walked to an inbound neighbor),
    /// then do not update the DB.
    /// Add this neighbor to our newly-calculated frontier either way
    /// Returns the updated `self.cur_neighbor` on success.
    /// Returns Err(..) if we failed to validate the request or we have a DB error.
    fn handle_handshake_accept(
        &mut self,
        network: &mut PeerNetwork,
        preamble: &Preamble,
        data: &HandshakeAcceptData,
        db_data: Option<&StackerDBHandshakeData>,
    ) -> Result<Neighbor, net_error> {
        let local_peer_str = format!("{:?}", network.get_local_peer());

        let mut neighbor_from_handshake = self
            .neighbor_db
            .neighbor_from_handshake(network, preamble, data)?;

        // if the neighbor accidentally gave us a private IP address, then
        // just use the one we used to contact it.  This can happen if the
        // node is behind a load-balancer, or is doing port-forwarding,
        // etc. But do nothing if both cur_neighbor and its reported address are private.
        if (neighbor_from_handshake.addr.addrbytes.is_in_private_range()
            || neighbor_from_handshake.addr.addrbytes.is_anynet())
            && !self.cur_neighbor.addr.addrbytes.is_in_private_range()
        {
            debug!(
                "{}: outbound neighbor gave private IP address {:?}; assuming it meant {:?}",
                local_peer_str, &neighbor_from_handshake.addr, &self.cur_neighbor.addr
            );
            neighbor_from_handshake.addr.addrbytes = self.cur_neighbor.addr.addrbytes.clone();
            neighbor_from_handshake.addr.port = self.cur_neighbor.addr.port;
        }

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

        debug!(
            "{}: Connected with {:?}",
            local_peer_str, &self.cur_neighbor.addr
        );

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

**File:** stackslib/src/net/neighbors/db.rs (L368-407)
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
```

**File:** stackslib/src/net/chat.rs (L475-516)
```rust
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

**File:** stackslib/src/net/p2p.rs (L4305-4394)
```rust
    fn schedule_network_pingbacks(&mut self, event_ids: Vec<usize>) {
        if cfg!(test) && self.connection_opts.disable_pingbacks {
            debug!("{:?}: pingbacks are disabled for testing", &self.local_peer);
            return;
        }

        // clear timed-out pingbacks
        let mut to_remove = vec![];
        for (naddr, pingback) in self.walk_pingbacks.iter() {
            if pingback.ts + self.connection_opts.pingback_timeout < get_epoch_time_secs() {
                to_remove.push((*naddr).clone());
            }
        }

        for naddr in to_remove.into_iter() {
            self.walk_pingbacks.remove(&naddr);
        }

        let my_pubkey_hash = Hash160::from_node_public_key(&Secp256k1PublicKey::from_private(
            &self.local_peer.private_key,
        ));

        // add new pingbacks
        for event_id in event_ids.into_iter() {
            if let Some(ref convo) = self.peers.get(&event_id) {
                if !convo.is_outbound() && convo.is_authenticated() {
                    let nk = convo.to_handshake_neighbor_key();
                    let addr = convo.to_handshake_neighbor_address();
                    let pubkey = convo
                        .get_public_key()
                        .expect("BUG: convo is authenticated but we have no public key for it");

                    if addr.public_key_hash == my_pubkey_hash {
                        // don't talk to ourselves
                        continue;
                    }

                    let neighbor_opt = PeerDB::get_peer(
                        self.peerdb.conn(),
                        self.local_peer.network_id,
                        &addr.addrbytes,
                        addr.port,
                    )
                    .expect("FATAL: failed to read from peer database");

                    if neighbor_opt.is_some() {
                        debug!(
                            "{:?}: will not ping back {:?}: already known to us",
                            &self.local_peer, &nk
                        );
                        continue;
                    }

                    debug!(
                        "{:?}: will ping back {:?} ({:?}) to see if it's routable from us",
                        &self.local_peer, &nk, convo
                    );
                    self.walk_pingbacks.insert(
                        addr,
                        NeighborPingback {
                            peer_version: nk.peer_version,
                            network_id: nk.network_id,
                            ts: get_epoch_time_secs(),
                            pubkey,
                        },
                    );

                    if self.walk_pingbacks.len() > MAX_NEIGHBORS_DATA_LEN as usize {
                        // drop one at random
                        let idx = thread_rng().gen::<usize>() % self.walk_pingbacks.len();
                        let drop_addr = match self.walk_pingbacks.keys().skip(idx).next() {
                            Some(addr) => (*addr).clone(),
                            None => {
                                continue;
                            }
                        };

                        debug!("{:?}: drop pingback {:?}", &self.local_peer, drop_addr);
                        self.walk_pingbacks.remove(&drop_addr);
                    }
                }
            }
        }

        debug!(
            "{:?}: have {} pingbacks scheduled",
            &self.local_peer,
            self.walk_pingbacks.len()
        );
    }
```
