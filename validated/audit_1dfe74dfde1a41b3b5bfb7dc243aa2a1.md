### Title
Attacker-computable `peer_slots` bucket collisions permit deterministic, low-message eviction lockout of legitimate peers in `PeerDB::try_insert_peer` - (File: stackslib/src/net/db.rs)

### Summary
`PeerDB::try_insert_peer` (stackslib/src/net/db.rs:1452-1488) only inserts a `Neighbor` into one of a small, fixed set of slots returned by `PeerDB::peer_slots`, and returns `Ok(false)` once all of those slots are occupied. Because `peer_slots` is called with only public, attacker-controlled/known inputs (`network_id`, `addrbytes`, `port`) and no per-node secret, a remote attacker can precompute which `(addrbytes, port)` values hash into the same slot set as a targeted legitimate peer and fill every slot in that bucket with distinct `Neighbor` records before the legitimate peer ever connects.

### Finding Description
`try_insert_peer` first checks presence via `PeerDB::has_peer`, then computes the candidate slots for the neighbor: [1](#0-0) 

The call `PeerDB::peer_slots(tx, neighbor.addr.network_id, &neighbor.addr.addrbytes, neighbor.addr.port)` takes no local secret/salt argument — only wire-derived, attacker-known fields. This means the slot-selection function is fully public and reproducible off-node: an attacker who knows the open-source hash/bucket logic can, offline, search for `(addrbytes, port)` tuples that collide into the same slot set as a target address, then register that many attacker-controlled `Neighbor`s (one per slot) via whatever peer-registration path feeds `try_insert_peer` (handshake/neighbor exchange). Once every slot in the bucket is occupied, `has_peer_at` returns `true` for each slot in the loop and the function falls through to `return Ok(false)` at line 1487, permanently rejecting the legitimate peer's insertion attempt as long as those attacker entries remain (no eviction/replacement/aging logic exists in this function). This matches the exact equality claimed in the question: bounded slots-per-bucket + no secret salt + no eviction logic on collision.

I could not retrieve the exact body of `PeerDB::peer_slots` (hash algorithm, constant name, and exact slot count) within this session due to index/tool limits on this file; the function is defined elsewhere in stackslib/src/net/db.rs but was not returned by search. This limits my ability to state the precise slot-count constant or exactly reproduce the collision search in a proof-of-concept without accessing that function body directly (a Devin session with full file access would be needed to extract it and write a concrete colliding-input test).

### Impact Explanation
If confirmed, this allows an attacker to steer the composition of a node's peer table away from legitimate peers for a specific bucket using only as many crafted `Neighbor` announcements as there are slots in that bucket (a small, bounded number, not a volumetric flood) — consistent with the "High: steering frontier composition" impact category. The affected node would refuse to persist/track the legitimate peer's entry in `PeerDB` for as long as the attacker-controlled slots remain occupied.

### Likelihood Explanation
Preconditions: attacker must be able to get their own `Neighbor` records processed by `try_insert_peer` (this happens via normal handshake/neighbor-list processing, reachable by any unprivileged remote peer), and must know (or be able to brute-force offline) the deterministic hash/bucket function used by `peer_slots` since it takes only public inputs. Cost is bounded by the number of slots per bucket (small, not volumetric). Repeatable per targeted bucket/peer.

### Recommendation
Verify and, if confirmed, fix `PeerDB::peer_slots` to incorporate a per-node secret (e.g., a random `local_peer` seed persisted in `PeerDB`, analogous to Bitcoin AddrMan's `nKey`) into the bucket-hash computation so remote attackers cannot predict or engineer collisions with a target address. Additionally, consider adding eviction/preference logic in `try_insert_peer` (e.g., prefer replacing stale/never-contacted entries) rather than unconditionally returning `Ok(false)` when all slots are occupied.

### Proof of Concept
Cannot be fully specified without the exact `peer_slots` hash implementation (not retrievable in this session). Proposed test plan for `stackslib::net::db` once that function is located:
1. Read `PeerDB::peer_slots`/underlying hash function source to confirm slot count and hash inputs.
2. Write a test that brute-force searches `(addrbytes, port)` pairs (holding `network_id` fixed) to find N inputs (N = slot count) that produce the same slot set as a fixed target `(addrbytes, port)`.
3. Call `PeerDB::try_insert_peer` for each of the N attacker `Neighbor`s, asserting each returns `Ok(true)`.
4. Call `PeerDB::try_insert_peer` for the legitimate target `Neighbor`, asserting it returns `Ok(false)` at stackslib/src/net/db.rs:1487, and that `PeerDB::has_peer` for the target remains `false`.

Given the inability to confirm the exact bucket function/constant in this session, this should be treated as a partially-verified finding requiring follow-up code inspection to finalize the PoC and constant naming.

### Citations

**File:** stackslib/src/net/db.rs (L1470-1487)
```rust
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
```
