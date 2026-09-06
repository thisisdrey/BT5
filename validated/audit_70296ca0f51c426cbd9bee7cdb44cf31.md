### Title
Unauthenticated address-claim in `Handshake` lets a remote attacker overwrite a legitimate peer's `PeerDB` row and drop its StackerDB associations - ([File: stackslib/src/net/chat.rs], [File: stackslib/src/net/db.rs], [File: stackslib/src/net/neighbors/db.rs])

### Summary
`NeighborKey::from_handshake` derives the PeerDB frontier lookup key `(addrbytes, port, network_id)` purely from the self-reported `handshake_data.addrbytes`/`port` field of an incoming `Handshake` message, never from the TCP socket's actual observed remote address. Because a peer's identity slot in `PeerDB` is keyed on that self-reported address rather than any proof of address ownership, any remote party can claim to be at the same `(addrbytes, port)` as a currently-known, legitimate peer and have `PeerDB::insert_or_replace_peer`/`update_peer` overwrite that peer's `public_key`, which also triggers `PeerDB::drop_stacker_dbs`.

### Finding Description
`NeighborKey::from_handshake` builds the key entirely from wire-controlled fields: [1](#0-0) 

`Neighbor::load_and_update` uses this claimed-address key to look up the existing `PeerDB` entry and merge/overwrite state with the handshake's `node_public_key`: [2](#0-1) [3](#0-2) 

This is invoked from `add_or_schedule_replace_neighbor` on receipt of a `Handshake`/`StackerDBHandshakeData` message, which then persists via `save_update`/`save` (eventually `PeerDB::update_peer` or `insert_or_replace_peer`), committing the transaction unconditionally when the (attacker-claimed) address is already known: [4](#0-3) 

`PeerDB::insert_or_replace_peer` performs a raw `INSERT OR REPLACE INTO frontier ... WHERE slot=?` keyed by the claimed `(addrbytes, port, network_id)`/slot, and if the stored public key differs from the previous occupant's, calls `PeerDB::drop_stacker_dbs`: [5](#0-4) 

The same clobber-and-drop behavior exists in the `update_peer` path used for already-known peers: [6](#0-5) 

The codebase itself acknowledges that the claimed handshake address and the real observed socket address are tracked as two separate fields (`peer_addrbytes`/`peer_port` "from socketaddr" vs `handshake_addrbytes`/`handshake_port` "from handshake"): [7](#0-6) 

and that for inbound connections the code explicitly does **not** reconcile the two, because NAT'd inbound peers legitimately cannot be validated against their true source address: [8](#0-7) 

The `Handshake` message's cryptographic signature only proves the sender controls the private key matching `node_public_key`; it proves nothing about ownership/reachability of the claimed `addrbytes`/`port`. Consequently, two different attacker-controlled connections, signed by two different keypairs, can each claim the same `(addrbytes, port)` as a legitimate currently-connected peer, and the second one's handshake will overwrite the first's `PeerDB` row (public key, `last_contact_time`, etc.) and drop the associated StackerDB replication set — with no verification tying the claimed address to the connecting party.

### Impact Explanation
An unauthenticated remote attacker can silently clobber a legitimate peer's identity in the local node's `PeerDB` `frontier` table — the authoritative mapping of `(addrbytes, port, network_id)` to a specific public key — by simply connecting and sending a `Handshake` that claims the victim's address with the attacker's own key. This is an unauthenticated write to persistent node state and, as a side effect, calls `PeerDB::drop_stacker_dbs`, wiping the StackerDB replication associations recorded for that slot. This can disrupt gossip/relay routing for the legitimate peer and its StackerDB replication set, and is repeatable across reconnects/slot churn. This matches the "unauthenticated/unauthorized write to state" Critical category.

### Likelihood Explanation
The attacker needs only network reachability to the node's P2P port (no privileged role, no secret) and knowledge of a currently-known peer's advertised `(addrbytes, port)` — information that is itself gossiped openly via `Neighbors`/`HandshakeAccept` messages, so it is trivially discoverable. The attack requires only completing a normal `Handshake` (cost: one TCP connection + one signed handshake message per attempt), and is fully repeatable.

### Recommendation
Do not derive/trust the frontier lookup key solely from the self-reported `handshake_data.addrbytes`/`port` for inbound connections. For inbound peers, bind the `PeerDB` slot to the actually-observed TCP source address (`peer_addrbytes`/`peer_port`) rather than the claimed address, or require some additional proof of address ownership/reachability (e.g., a reachability probe / outbound-confirmed dial-back) before allowing a `Handshake` from a new key to overwrite an existing `(addrbytes, port)` slot's public key and drop its StackerDB associations.

### Proof of Concept
Rust test in `stackslib::net::db` (module `tests`) analogous to existing PeerDB tests:
1. Build `Neighbor` A with `NeighborKey{addrbytes: X, port: P, network_id: N}` and `public_key = pk_A`, insert into a slot via `PeerDB::insert_or_replace_peer(&tx, &neighbor_a, slot)`, and separately call `PeerDB::insert_or_replace_stacker_dbs` for that slot to simulate a peer with active StackerDB associations.
2. Build `Neighbor` B with the *same* `NeighborKey{addrbytes: X, port: P, network_id: N}` but `public_key = pk_B` (different keypair, no relation to A).
3. Call `PeerDB::insert_or_replace_peer(&tx, &neighbor_b, slot)`.
4. Assert: `PeerDB::get_peer_at(&tx, N, slot).unwrap().public_key.to_bytes_compressed() == pk_B.to_bytes_compressed()` (silently overwrote A's key), and `PeerDB::get_stacker_dbs_by_slot(&tx, slot)` (or equivalent query on `stackerdb_peers`) returns empty (associations dropped), demonstrating the clobber described at `stackslib/src/net/db.rs:1121-1129`.

### Citations

**File:** stackslib/src/net/chat.rs (L342-349)
```rust
    /// from socketaddr
    pub peer_addrbytes: PeerAddress,
    /// from socketaddr
    pub peer_port: u16,
    /// from handshake
    pub handshake_addrbytes: PeerAddress,
    /// from handshake
    pub handshake_port: u16,
```

**File:** stackslib/src/net/chat.rs (L412-423)
```rust
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

**File:** stackslib/src/net/chat.rs (L438-465)
```rust
    pub fn handshake_update(
        &mut self,
        conn: &DBConn,
        handshake_data: &HandshakeData,
    ) -> Result<(), net_error> {
        let pubk = handshake_data
            .node_public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;
        let asn_opt =
            PeerDB::asn_lookup(conn, &handshake_data.addrbytes).map_err(net_error::DBError)?;

        let asn = match asn_opt {
            Some(a) => a,
            None => 0,
        };

        self.public_key = pubk;
        self.expire_block = handshake_data.expire_block_height;
        self.last_contact_time = get_epoch_time_secs();

        if asn != 0 {
            self.asn = asn;
            self.org = asn; // TODO; AS number is a place-holder for an organization ID (an organization can own multiple ASs)
        }

        Ok(())
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

**File:** stackslib/src/net/db.rs (L1090-1133)
```rust
    /// Insert or replace a neighbor into a given slot
    pub fn insert_or_replace_peer(
        tx: &Transaction,
        neighbor: &Neighbor,
        slot: u32,
    ) -> Result<(), db_error> {
        let old_peer_opt = PeerDB::get_peer_at(tx, neighbor.addr.network_id, slot)?;

        let neighbor_args = params![
            neighbor.addr.peer_version,
            neighbor.addr.network_id,
            to_bin(neighbor.addr.addrbytes.as_bytes()),
            neighbor.addr.port,
            to_hex(&neighbor.public_key.to_bytes_compressed()),
            u64_to_sql(neighbor.expire_block)?,
            u64_to_sql(neighbor.last_contact_time)?,
            neighbor.asn,
            neighbor.org,
            neighbor.allowed,
            neighbor.denied,
            neighbor.in_degree,
            neighbor.out_degree,
            0i64,
            slot,
            !neighbor.addr.addrbytes.is_in_private_range()
        ];

        tx.execute("INSERT OR REPLACE INTO frontier (peer_version, network_id, addrbytes, port, public_key, expire_block_height, last_contact_time, asn, org, allowed, denied, in_degree, out_degree, initial, slot, public) \
                   VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)", neighbor_args)
            .map_err(db_error::SqliteError)?;

        if let Some(old_peer) = old_peer_opt {
            if old_peer.addr != neighbor.addr
                || old_peer.public_key.to_bytes_compressed()
                    != neighbor.public_key.to_bytes_compressed()
            {
                // the peer for this slot changed. Drop the associated stacker DB records
                debug!("Peer at slot {} changed; dropping its DBs", slot);
                PeerDB::drop_stacker_dbs(tx, slot)?;
            }
        }

        Ok(())
    }
```

**File:** stackslib/src/net/db.rs (L1285-1333)
```rust
    /// Update an existing peer's entries.  Does nothing if the peer is not present.
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

        if let Some(old_peer) = old_peer_opt {
            let slot_opt = Self::find_peer_slot(
                tx,
                neighbor.addr.network_id,
                &neighbor.addr.addrbytes,
                neighbor.addr.port,
            )?;
            if old_peer.public_key.to_bytes_compressed()
                != neighbor.public_key.to_bytes_compressed()
            {
                // this peer has re-keyed, so it might be a new peer altogether.
                // require it to re-announce its DBs
                if let Some(slot) = slot_opt {
                    debug!("Peer at slot {} changed; dropping its DBs", slot);
                    PeerDB::drop_stacker_dbs(tx, slot)?;
                }
            }
        }
        Ok(())
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
