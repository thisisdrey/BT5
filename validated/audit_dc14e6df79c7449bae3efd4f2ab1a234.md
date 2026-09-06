### Title
Unauthenticated StackerDB replica-set poisoning via self-declared `smart_contracts` in handshake data - ([File: stackslib/src/net/db.rs])

### Summary
`PeerDB::try_insert_peer` and `PeerDB::update_peer_stacker_dbs` write the `stacker_dbs` list into the `stackerdb_peers` table with no verification that the peer being inserted actually owns a slot in those StackerDB contracts. This list originates from the peer's self-declared `StackerDBHandshakeData.smart_contracts` at handshake time, so any remote peer can claim to replicate any contract's StackerDB and have that claim persisted as authoritative metadata.

### Finding Description
`PeerDB::insert_or_replace_stacker_dbs` unconditionally inserts `(smart_contract_id, peer_slot)` rows for whatever `smart_contracts` list is passed in, with no cross-check against the contract's actual slot-owner list: [1](#0-0) 

This is called from `try_insert_peer`, which inserts/updates a neighbor's frontier entry and then blindly records the caller-supplied `stacker_dbs` for that peer's slot: [2](#0-1) 

Similarly, for already-known peers, `update_peer_stacker_dbs` computes a diff and inserts/deletes rows purely based on the new `dbs` slice passed in, again without any check against slot ownership: [3](#0-2) 

The `stacker_dbs`/`dbs` argument in both cases is populated from `StackerDBHandshakeData.smart_contracts`, which is data the peer supplies itself as part of the handshake payload (confirmed present in `codec.rs`, `chat.rs`, `neighbors/db.rs`, and `neighbors/walk.rs`). The P2P handshake signature only authenticates that the message came from the claimed public key — it says nothing about whether that key's owner holds a slot in any of the named StackerDB contracts. There is no lookup against the contract's on-chain/off-chain slot-owner list before the `INSERT OR REPLACE` into `stackerdb_peers`.

### Impact Explanation
An attacker-controlled peer can, via a single valid handshake, insert arbitrary `(smart_contract_id, peer_slot)` rows into the target node's `stackerdb_peers` table for any StackerDB contract, without holding a slot in it. This table is consulted by StackerDB sync/replica-discovery logic (`stackerdb/sync.rs`, `api/liststackerdbreplicas.rs`) to determine which peers to query for chunks of a given contract's StackerDB. Poisoning this metadata lets an attacker insert itself into the discovered replica set for contracts it doesn't participate in, which can be used to waste sync bandwidth/requests on a bogus source, and can propagate through neighbor-info gossip (`neighbors/db.rs`, `neighbors/walk.rs`) to other nodes that build their view of replicas partly from this and other peers' claims. This constitutes an unauthenticated write of trust-relevant network metadata, satisfying the "unauthenticated/unauthorized write to state" Critical category — the effect is on peer discovery/sync steering, not the StackerDB contents/slot data itself (chunk contents remain protected by the chunk-level slot-owner signature checks in the chunk-push/replication protocol, which is unaffected by this bug).

### Likelihood Explanation
Any remote peer able to complete a normal P2P handshake (only requires a valid handshake signature over its own keypair — no privileged role, no proof of slot ownership) can trigger this on every handshake it performs, at zero cost beyond a standard handshake. It is fully repeatable per-connection/per-reconnect and requires no special node state beyond the target running the standard P2P stack with StackerDB support.

### Recommendation
Before persisting `smart_contracts`/`stacker_dbs` claims from a handshake, validate them against the actual slot-owner list of each named `QualifiedContractIdentifier` (e.g., by checking the contract's registered signer/slot-owner set, similar to how chunk pushes are validated), and drop/ignore any claimed contract for which the peer does not hold a slot. Alternatively, treat handshake-declared `smart_contracts` as advisory/unverified and require corroboration via at least one successfully validated chunk exchange before trusting the peer as a replica source in sync/discovery logic.

### Proof of Concept
Add a test in `stackslib/src/net/db.rs`'s test module that:
1. Sets up a `PeerDB` and defines a `QualifiedContractIdentifier` `cid` for which no slot-owner registration exists for the attacker's key.
2. Constructs an attacker `Neighbor` with an arbitrary public key/address.
3. Calls `PeerDB::try_insert_peer(&tx, &attacker_neighbor, &[cid.clone()])` directly (simulating what `chat.rs`'s handshake-accept path would do with a self-declared `StackerDBHandshakeData`), with no prior slot-ownership fact established anywhere in the DB.
4. Asserts the call returns `Ok(true)` and that `PeerDB::get_stacker_dbs_by_slot`/`static_get_peer_stacker_dbs` for the attacker's slot returns `cid`, proving the association was recorded despite no verification of actual slot ownership — the equality "recorded replicas == proven replicas" fails.

### Citations

**File:** stackslib/src/net/db.rs (L1068-1081)
```rust
    /// Insert or replace stacker DB contract IDs for a peer, given its slot
    pub fn insert_or_replace_stacker_dbs(
        tx: &Transaction,
        slot: u32,
        smart_contracts: &[QualifiedContractIdentifier],
    ) -> Result<(), db_error> {
        for cid in smart_contracts {
            test_debug!("Add Stacker DB contract to slot {}: {}", slot, cid);
            let args = params![cid.to_string(), slot];
            tx.execute("INSERT OR REPLACE INTO stackerdb_peers (smart_contract_id,peer_slot) VALUES (?1,?2)", args)
                .map_err(db_error::SqliteError)?;
        }
        Ok(())
    }
```

**File:** stackslib/src/net/db.rs (L1403-1445)
```rust
    /// Update an existing peer's stacker DB IDs.
    /// Calculates the delta between what's in the DB now, and what's in `dbs`, and deletes the
    /// records absent from `dbs` and adds records not present in the DB.
    /// Does nothing if the peer is not present.
    pub fn update_peer_stacker_dbs(
        tx: &Transaction,
        neighbor: &Neighbor,
        dbs: &[QualifiedContractIdentifier],
    ) -> Result<(), db_error> {
        let slot = if let Some(slot) = PeerDB::find_peer_slot(
            tx,
            neighbor.addr.network_id,
            &neighbor.addr.addrbytes,
            neighbor.addr.port,
        )? {
            slot
        } else {
            return Ok(());
        };
        let cur_dbs_set: HashSet<_> = PeerDB::static_get_peer_stacker_dbs(tx, neighbor)?
            .into_iter()
            .collect();
        let new_dbs_set: HashSet<QualifiedContractIdentifier> = dbs.iter().cloned().collect();
        let to_insert: Vec<_> = new_dbs_set.difference(&cur_dbs_set).collect();
        let to_delete: Vec<_> = cur_dbs_set.difference(&new_dbs_set).collect();

        let sql = "DELETE FROM stackerdb_peers WHERE smart_contract_id = ?1 AND peer_slot = ?2";
        for cid in to_delete.into_iter() {
            test_debug!("Delete Stacker DB for {:?}: {}", &neighbor.addr, &cid);
            let args = params![cid.to_string(), slot];
            tx.execute(sql, args).map_err(db_error::SqliteError)?;
        }

        let sql =
            "INSERT OR REPLACE INTO stackerdb_peers (smart_contract_id,peer_slot) VALUES (?1,?2)";
        for cid in to_insert.iter() {
            test_debug!("Add Stacker DB for {:?}: {}", &neighbor.addr, &cid);
            let args = params![cid.to_string(), slot];
            tx.execute(sql, args).map_err(db_error::SqliteError)?;
        }

        Ok(())
    }
```

**File:** stackslib/src/net/db.rs (L1452-1488)
```rust
    pub fn try_insert_peer(
        tx: &Transaction,
        neighbor: &Neighbor,
        stacker_dbs: &[QualifiedContractIdentifier],
    ) -> Result<bool, db_error> {
        let present = PeerDB::has_peer(
            tx,
            neighbor.addr.network_id,
            &neighbor.addr.addrbytes,
            neighbor.addr.port,
        )?;
        if present {
            // already here
            PeerDB::update_peer(tx, neighbor)?;
            PeerDB::update_peer_stacker_dbs(tx, neighbor, stacker_dbs)?;
            return Ok(true);
        }

        let slots = PeerDB::peer_slots(
            tx,
            neighbor.addr.network_id,
            &neighbor.addr.addrbytes,
            neighbor.addr.port,
        )?;
        for slot in slots.iter() {
            let used_slot = PeerDB::has_peer_at(tx, neighbor.addr.network_id, *slot)?;
            if !used_slot {
                // have a spare slot!
                PeerDB::insert_or_replace_peer(tx, neighbor, *slot)?;
                PeerDB::insert_or_replace_stacker_dbs(tx, *slot, stacker_dbs)?;
                return Ok(true);
            }
        }

        // no slots free
        return Ok(false);
    }
```
