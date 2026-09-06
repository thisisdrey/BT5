### Title
`PeerDBNeighborWalk::find_replaced_neighbor_slot` selects eviction candidates without excluding `allowed` (operator-trusted) peers - ([File: stackslib/src/net/neighbors/db.rs])

### Summary
When a remote handshake collides with an existing frontier slot, `find_replaced_neighbor_slot` picks a random candidate slot from `PeerDB::peer_slots` without checking whether the occupant is an operator-trusted (`allowed=1`) peer. The subsequent `replace_neighbors` step only checks whether the *incoming* attacker address is denied, never whether the *outgoing* occupant is allowed, so a trusted peer's frontier row can be silently overwritten.

### Finding Description
`find_replaced_neighbor_slot` computes candidate slots via `PeerDB::peer_slots`, shuffles them, and returns the first one with no filtering: [1](#0-0) 

It is invoked from `add_or_schedule_replace_neighbor` only after `neighbor_from_handshake.save()` (i.e. `PeerDB::try_insert_peer`) fails because the frontier bucket is full: [2](#0-1) 

The chosen slot is stored via `NeighborReplacements::add_neighbor` without ever consulting the occupant's `allowed` field: [3](#0-2) 

Later, `replace_neighbors` performs the actual overwrite. It fetches the occupant (`replaced`) and checks only whether the *new* attacker address is denied — it never checks `replaced.allowed`: [4](#0-3) 

`insert_or_replace_peer` then unconditionally overwrites the row (`INSERT OR REPLACE`) at that slot, including dropping the old peer's StackerDB replication state if the address/key changed: [5](#0-4) 

So the invariant that "allowed==1 peers are never eviction candidates" is not enforced anywhere in this path: not in slot selection (`find_replaced_neighbor_slot`), and not in the commit step (`replace_neighbors`). An attacker who can get a handshake's candidate slot set (`PeerDB::peer_slots`, keyed on network_id/addrbytes/port and a secret local nonce) to collide with the slot currently occupied by a trusted/allowed peer can have that trusted peer's frontier row silently replaced by the attacker's own untrusted `Neighbor` record.

Note: `PeerDB::peer_slots` mixes in `local_peer.nonce`, a value not known to the remote attacker, so engineering a guaranteed collision with a *specific* target requires either guessing/brute-forcing across the local hash space or relying on chance collisions as the frontier fills up; this affects exploit cost/likelihood but not the underlying code fault, which lacks any allowed-peer exclusion regardless of how the collision is produced.

### Impact Explanation
If exploited, an unprivileged remote peer can cause the operator-trusted neighbor's `frontier` row (its address, public key, and StackerDB replication entries) to be overwritten with the attacker-controlled neighbor's data, and the trusted peer is reported as `DropReason::ReplacedConnection`. This is an unauthorized write to persistent P2P state that bypasses the explicit "allowed" trust designation the operator configured, matching the Critical category of "unauthenticated/unauthorized write to state."

### Likelihood Explanation
This requires: (1) an existing frontier bucket that is at capacity so `save()` fails, (2) the attacker's crafted `(network_id, addrbytes, port)` hashing (with the target's secret local nonce baked into `PeerDB::peer_slots`) to produce a slot value equal to one already occupied by the trusted peer. Because the nonce is unknown to the attacker, this is a probabilistic/brute-force attack requiring many distinct handshakes from many source addresses/ports (feasible from a controlled `/24` or larger range as described), not a single deterministic message. The remote handshake path itself is reachable by any unprivileged peer with no secret or privileged role required.

### Recommendation
In `find_replaced_neighbor_slot`, filter out (or deprioritize) any slot whose occupant has `allowed != 0` (and not expired) before shuffling/selecting a candidate, using `PeerDB::get_peer_at` to inspect the occupant. Additionally, in `replace_neighbors`, add an explicit guard that skips replacement (`continue`) when `replaced.allowed` indicates the occupant is currently trusted, mirroring the existing `is_address_denied` guard but for the *outgoing* peer instead of only the incoming one.

### Proof of Concept
Rust test plan in `stackslib/src/net/neighbors/db.rs` (or a new test module colocated with existing `PeerDB` tests):
1. Build an in-memory `PeerDB`, insert a legitimate neighbor `trusted` with `allowed = -1` (always-allowed) at slot `S` via `PeerDB::insert_or_replace_peer`.
2. Insert an untrusted neighbor `attacker_seed` at another slot in the same `PeerDB::peer_slots` bucket for a crafted `(network_id, addrbytes, port)` so both `trusted` and `attacker_seed` are candidates returned by `PeerDB::peer_slots` for that key (achievable deterministically in a test by controlling the local peer's nonce directly, since it's stored in the DB and readable/writable in tests).
3. Call `PeerDBNeighborWalk::find_replaced_neighbor_slot(&conn, &nk)` repeatedly (e.g., 100 iterations) and assert that it never returns slot `S` (the trusted peer's slot) — i.e., `assert!(returned_slots.iter().all(|s| *s != trusted_slot))`.
4. Demonstrate the current code fails this assertion (returns `S` with a probability determined by shuffle over the bucket), proving the fault; after applying the recommended fix (excluding `allowed` occupants), the assertion should hold with 0 occurrences of `S`.

### Citations

**File:** stackslib/src/net/neighbors/db.rs (L41-52)
```rust
impl NeighborReplacements {
    pub fn new() -> NeighborReplacements {
        NeighborReplacements {
            replacements: HashMap::new(),
            replaced_neighbors: HashMap::new(),
        }
    }

    pub fn add_neighbor(&mut self, naddr: NeighborAddress, neighbor: Neighbor, slot: u32) {
        self.replacements.insert(naddr.clone(), neighbor);
        self.replaced_neighbors.insert(naddr, slot);
    }
```

**File:** stackslib/src/net/neighbors/db.rs (L287-302)
```rust
    fn find_replaced_neighbor_slot(
        conn: &DBConn,
        nk: &NeighborKey,
    ) -> Result<Option<u32>, net_error> {
        let mut slots = PeerDB::peer_slots(conn, nk.network_id, &nk.addrbytes, nk.port)
            .map_err(net_error::DBError)?;

        if slots.is_empty() {
            // not present
            return Ok(None);
        }

        let mut rng = thread_rng();
        slots.shuffle(&mut rng);
        Ok(slots.first().copied())
    }
```

**File:** stackslib/src/net/neighbors/db.rs (L400-420)
```rust
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
```

**File:** stackslib/src/net/neighbors/db.rs (L463-502)
```rust
    fn replace_neighbors(
        &self,
        network: &mut PeerNetwork,
        replacements: &NeighborReplacements,
        result: &mut NeighborWalkResult,
    ) -> Result<(), net_error> {
        let network_id = network.bound_neighbor_key().network_id;
        let local_peer_str = format!("{:?}", network.get_local_peer());

        let tx = network.peerdb_tx_begin()?;
        for (replaceable_naddr, slot) in replacements.iter_slots() {
            let replacement = match replacements.get_neighbor(replaceable_naddr) {
                Some(n) => n,
                None => {
                    continue;
                }
            };

            let replaced_opt = PeerDB::get_peer_at(&tx, network_id, *slot)?;
            if let Some(replaced) = replaced_opt {
                if PeerDB::is_address_denied(&tx, &replacement.addr.addrbytes)? {
                    debug!(
                        "{:?}: Will not replace {:?} with {:?} -- is denied",
                        local_peer_str, &replaced.addr, &replacement.addr
                    );
                    continue;
                }
                debug!(
                    "{:?}: Replace {:?} with {:?}",
                    local_peer_str, &replaced.addr, &replacement.addr
                );

                PeerDB::insert_or_replace_peer(&tx, replacement, *slot)?;
                result.add_replaced(DropNeighbor {
                    key: replaced.addr.clone(),
                    reason: DropReason::ReplacedConnection,
                    source: DropSource::NeighborWalkPeerDB,
                });
            }
        }
```

**File:** stackslib/src/net/db.rs (L1091-1130)
```rust
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
```
