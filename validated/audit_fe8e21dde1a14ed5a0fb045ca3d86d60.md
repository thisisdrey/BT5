### Title
PeerDB writes during neighbor-of-neighbor / pingback handshakes are keyed by self-reported `HandshakeData.addrbytes/port`, allowing address-spoofed PeerDB poisoning - (File: stackslib/src/net/db.rs, stackslib/src/net/chat.rs, stackslib/src/net/neighbors/walk.rs)

### Summary
`PeerDB::try_insert_peer`/`update_peer` and `Neighbor::load_and_update` key every write purely on the `(network_id, addrbytes, port)` tuple taken from the handshake's self-reported `HandshakeData.addrbytes`/`port` fields, not from the actually-dialed or actually-observed TCP peer address. While the direct outbound-handshake path (`ConversationP2P::validate_handshake`) and the single "current neighbor" walk step (`NeighborWalk::handle_handshake_accept`) both cross-check the reported address against the real connection address, the "handshake neighbors-of-neighbors" path (`neighbor_handshakes_try_finish`/`add_or_schedule_replace_neighbor`) and the pingback path (`pingback_handshakes_try_finish`/`add_or_schedule_replace_neighbor`) never perform that check, so a remote peer that completes a handshake with its own real key can still get a PeerDB row keyed by an arbitrary (e.g. victim's) IP:port overwritten with its own public key.

### Finding Description
Every PeerDB neighbor row is keyed by `NeighborKey` built from the handshake payload: [1](#0-0) 

`Neighbor::load_and_update` looks up/creates a row using this handshake-derived key and unconditionally sets `self.public_key` from the handshake's key material: [2](#0-1) 

The actual DB write, `PeerDB::update_peer`, updates the `public_key` column filtered only by `(network_id, addrbytes, port)` — with no check that the previous occupant of that row was ever contacted at that address, and no check that the incoming write's address matches the address the connection was actually made to/from: [3](#0-2) 

For the neighbor-of-neighbor crawl (`GetHandshakesFinish` state), the code dials each advertised `NeighborAddress` (`naddr`), but when processing the reply it hands `&data.handshake` (the remote peer's *self-reported* address/pubkey) straight to `add_or_schedule_replace_neighbor` without ever comparing `data.handshake.addrbytes/port` to `naddr`: [4](#0-3) [5](#0-4) 

The pingback path is similar: `check_handshake_pubkey_hash` only verifies that the pubkey-hash in the handshake matches the expected `naddr.public_key_hash`; it never checks that `data.handshake.addrbytes/port` equals the actually-dialed `naddr`: [6](#0-5) 

By contrast, the two paths that *do* enforce this equality are (1) `validate_handshake`, which only checks address match for **outbound** connections when a public key is **already known**: [7](#0-6) 

and (2) the walk's single "current neighbor" step, which rejects a mismatched self-reported address for `walk_outbound`: [8](#0-7) 

Neither of these covers `add_or_schedule_replace_neighbor`, which is the function that actually persists neighbor-of-neighbor and pingback handshake results to PeerDB.

**Exploit flow:** Attacker legitimately becomes a "neighbor of a neighbor" of victim node X (i.e., some peer Y that X is crawling truthfully reports attacker's real reachable `attacker_ip:attacker_port` to X via `GetNeighbors`). X dials `attacker_ip:attacker_port` (this is `naddr`), sends a `Handshake`, and gets back a `HandshakeAccept`/`StackerDBHandshakeAccept` that attacker crafts with `handshake.addrbytes = victim_ip`, `handshake.port = victim_port` (a completely different address than `naddr`, e.g. a real third-party victim's IP). Because attacker signed this message with its own real private key, the signature check passes trivially — the vulnerability is not in authentication of the *message*, but in the fact that the *address used as the PeerDB row key* is taken from unauthenticated, self-reported payload fields rather than the verified connection endpoint. X's `add_or_schedule_replace_neighbor` then calls `Neighbor::load_and_update`/`try_insert_peer`/`update_peer`, which inserts or overwrites the row for `(network_id, victim_ip, victim_port)` with attacker's public key.

### Impact Explanation
X's PeerDB now contains a forged mapping: `victim_ip:victim_port -> attacker_pubkey`. Because PeerDB rows feed `getneighbors`/`GetNeighbors` responses served to any other crawling peer, this forged record propagates network-wide as X gossips it to further peers, who may themselves write/overwrite their own PeerDB rows the same way when they crawl X. This is an unauthenticated write to persisted node state and network-wide propagation of forged neighbor data — matching the Critical impact category. It can also silently overwrite a previously-legitimate PeerDB entry for the real victim (if X had it), disrupting X's ability to correctly re-contact/authenticate the real victim, and can be repeated for any number of addr/port pairs an attacker chooses, at negligible cost (one handshake reply per target address).

### Likelihood Explanation
The attacker needs no special privilege: any unprivileged remote party that runs a real peer able to complete a handshake with X (directly or by being reachable when X crawls a route to it) can trigger this. No RPC secret, signer role, or slot ownership is required — only the ability to answer a P2P handshake with a crafted payload. Precondition: X must discover the attacker as a "neighbor of a neighbor" (or issue a pingback) and dial it during a walk step, which is routine, automatic P2P crawling behavior in Stacks nodes and requires no victim/target complicity.

### Recommendation
In `add_or_schedule_replace_neighbor`/`NeighborWalkDB` implementations used for neighbor-of-neighbor and pingback handshakes, verify that `handshake.addrbytes`/`port` equals the address that was actually dialed (`naddr`) before calling `Neighbor::load_and_update`/`save`/`save_update`, exactly as is already done in `NeighborWalk::handle_handshake_accept` for the primary walk target. Alternatively, always key PeerDB writes by the verified connection endpoint address rather than by self-reported `HandshakeData` fields, falling back to the self-reported address only when it is provably unroutable/private (as is already special-cased) and only after confirming the reported pubkey hash also matches an expected value bound to that connection.

### Proof of Concept
Add a `stackslib::net::neighbors::walk` test modeled on the existing `neighbor_handshakes_try_finish` flow:
1. Set up node X's `PeerNetwork`/`PeerDB` and a walk state in `NeighborWalkState::GetHandshakesFinish` with `cur_neighbor` = Y and one pending neighbor-of-neighbor address `naddr = (attacker_ip, attacker_port, attacker_pubkey_hash)`.
2. Simulate `comms.collect_replies` returning a `StacksMessageType::HandshakeAccept` whose `HandshakeAcceptData.handshake` is signed by the attacker's real key but has `addrbytes = victim_ip`, `port = victim_port` (differing from `naddr`).
3. Call `NeighborWalk::neighbor_handshakes_try_finish(&mut network)`.
4. Assert (bug reproduction): `PeerDB::get_peer(peerdb_conn, network_id, &victim_ip, victim_port)` returns `Some(Neighbor { public_key: attacker_pubkey, .. })` — i.e., X's PeerDB now has an entry keyed by the victim's address holding the attacker's key, even though X never dialed or received a connection from `victim_ip:victim_port`.
5. Fix verification: after patching `add_or_schedule_replace_neighbor`/`handle_neighbor_handshake_accept` to require `data.handshake.addrbytes == naddr.addrbytes && data.handshake.port == naddr.port`, re-run the test and assert the write is rejected/no PeerDB row is created for `victim_ip:victim_port`.

### Citations

**File:** stackslib/src/net/chat.rs (L411-423)
```rust
impl NeighborKey {
    pub fn from_handshake(
        peer_version: u32,
        network_id: u32,
        handshake_data: &HandshakeData,
    ) -> NeighborKey {
        NeighborKey {
            peer_version,
            network_id,
            addrbytes: handshake_data.addrbytes.clone(),
            port: handshake_data.port,
        }
    }
```

**File:** stackslib/src/net/chat.rs (L475-500)
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
```

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

**File:** stackslib/src/net/db.rs (L1286-1313)
```rust
    pub fn update_peer(tx: &Transaction, neighbor: &Neighbor) -> Result<(), db_error> {
        let old_peer_opt = PeerDB::get_peer(
            tx,
            neighbor.addr.network_id,
            &neighbor.addr.addrbytes,
            neighbor.addr.port,
        )?;

        let args = params![
            neighbor.addr.peer_version,
            to_hex(&neighbor.public_key.to_bytes_compressed()),
            u64_to_sql(neighbor.expire_block)?,
            u64_to_sql(neighbor.last_contact_time)?,
            neighbor.asn,
            neighbor.org,
            neighbor.allowed,
            neighbor.denied,
            neighbor.in_degree,
            neighbor.out_degree,
            !neighbor.addr.addrbytes.is_in_private_range(),
            neighbor.addr.network_id,
            to_bin(neighbor.addr.addrbytes.as_bytes()),
            neighbor.addr.port,
        ];

        tx.execute("UPDATE frontier SET peer_version = ?1, public_key = ?2, expire_block_height = ?3, last_contact_time = ?4, asn = ?5, org = ?6, allowed = ?7, denied = ?8, in_degree = ?9, out_degree = ?10, public = ?11 \
                    WHERE network_id = ?12 AND addrbytes = ?13 AND port = ?14", args)
            .map_err(db_error::SqliteError)?;
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
