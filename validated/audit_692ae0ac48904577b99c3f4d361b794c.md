### Title
Unauthenticated PeerDB frontier poisoning via forged address in pingback HandshakeAccept - (File: stackslib/src/net/neighbors/walk.rs)

### Summary
`pingback_handshakes_try_finish` accepts a peer's self-reported `addrbytes`/`port` inside a `HandshakeAccept`/`StackerDBHandshakeAccept` and passes it straight to `add_or_schedule_replace_neighbor`, which writes it into `PeerDB` via `Neighbor::load_and_update`/`save`/`save_update` → `PeerDB::try_insert_peer`. The only check performed (`check_handshake_pubkey_hash`) validates the public key hash, never the address, so an attacker who is dialed back during a pingback probe can claim an arbitrary third-party `(addrbytes, port)` under a key they control.

### Finding Description
`validate_handshake` (stackslib/src/net/chat.rs:1047-1092) only enforces that `handshake_data.addrbytes`/`port` match the actual TCP peer address for **outbound** connections handling an inbound *Handshake request*, and even that check is skipped when the claimed address `is_anynet()`. This gate is irrelevant to the pingback flow because pingback never routes through `validate_handshake` at all.

The pingback flow works as follows:
1. A remote peer makes an inbound connection to the victim and is registered as a pingback candidate using the real, TCP-verified socket address.
2. The victim dials back out to that verified address (a genuinely-outbound connection) and sends a `Handshake`.
3. The attacker's listener replies with a `HandshakeAccept`/`StackerDBHandshakeAccept` whose embedded `HandshakeData.addrbytes`/`port` fields are attacker-chosen wire content — not required to equal the address the victim just dialed.
4. `pingback_handshakes_try_finish` (stackslib/src/net/neighbors/walk.rs:1683-1725) processes this reply directly: it calls `check_handshake_pubkey_hash(&peer_nk, data, &naddr)` [1](#0-0)  which validates only the public-key hash, then immediately calls `self.neighbor_db.add_or_schedule_replace_neighbor(network, &message.preamble, &data.handshake, db_data, ...)` [2](#0-1)  with no address cross-check against `naddr`/the dialed socket.
5. `add_or_schedule_replace_neighbor` calls `Neighbor::load_and_update(&tx, ..., handshake)` and then `neighbor_from_handshake.save(...)` [3](#0-2) . `Neighbor::load_and_update` builds the DB key straight from the untrusted field: `let addr = NeighborKey::from_handshake(peer_version, network_id, handshake_data);` [4](#0-3) .
6. `Neighbor::save` calls `PeerDB::try_insert_peer(tx, self, stacker_dbs...)` [5](#0-4) , which, if a slot is free for that `(network_id, addrbytes, port)`, inserts the row via `PeerDB::insert_or_replace_peer` [6](#0-5) .

Critically, the sibling code path for ordinary outbound walks, `handle_handshake_accept`, does contain an address-consistency check: `if self.walk_outbound && neighbor_from_handshake.addr != self.cur_neighbor.addr { ... return Err(...) }` [7](#0-6) . But `pingback_handshakes_try_finish` never calls `handle_handshake_accept` — it calls `add_or_schedule_replace_neighbor` directly — so this guard never applies to pingback replies, regardless of `walk_outbound`'s value.

Separately, the question's literal `handle_handshake` path is **not** exploitable on inbound: the write-to-DB branch in `handle_handshake` is gated by `if updated && self.stats.outbound` [8](#0-7) , meaning inbound handshake requests never trigger this particular store; only outbound re-keys do, and those addresses are already validated by `validate_handshake`'s outbound check (aside from the anynet wildcard exemption, which is not an attacker-chosen third-party address).

### Impact Explanation
An attacker who accepts an inbound connection from a victim node and is later pingback-dialed can, with a single crafted reply, cause the victim to write an arbitrary `(network_id, addrbytes, port)` tuple bound to an attacker-controlled public key into the victim's `PeerDB` frontier — an unauthenticated write to persistent P2P state. Because `PeerDB` entries are later gossiped to other peers during neighbor-walk `Neighbors` exchanges, this poisons neighbor discovery for third parties who query the victim's frontier, matching the Critical category ("unauthenticated write to state" / "network-wide propagation of forged data").

### Likelihood Explanation
Preconditions: attacker only needs to accept a normal inbound P2P connection from the target (any unprivileged remote party can do this by running a reachable listener) and respond to the resulting pingback dial with a forged `HandshakeAccept`. No secret, no privileged role, and no special peer state is required beyond an available frontier slot for the claimed `(network_id, addrbytes, port)`, which is a routine condition. The attack is repeatable per pingback cycle and costs only running a listening server.

### Recommendation
In `pingback_handshakes_try_finish`, before calling `add_or_schedule_replace_neighbor`, verify that `data.handshake.addrbytes`/`port` equal the actual dialed/connected peer address (`naddr`/`peer_nk`) for non-anynet claims, mirroring the check already present in `handle_handshake_accept` (stackslib/src/net/neighbors/walk.rs:705-715), and apply it unconditionally rather than only when `self.walk_outbound` is true.

### Proof of Concept
Rust test plan in `stackslib/src/net/neighbors/walk.rs` test module (or `tests/neighbors.rs`):
1. Set up a victim `PeerNetwork`/`PeerDB` and a mock inbound `Convo` from an attacker socket `A`.
2. Register `A` as a pingback candidate (simulate reaching `NeighborWalkState::PingbackHandshakesFinish` with `naddr` = attacker's real address and a known attacker pubkey hash).
3. Simulate the outbound pingback dial's reply: craft a `StacksMessage` with `StacksMessageType::HandshakeAccept(HandshakeAcceptData { handshake: HandshakeData { addrbytes: <victim-chosen third-party IP>, port: <arbitrary>, node_public_key: <attacker key>, .. }, .. })`, signed by the attacker's key (satisfies `check_handshake_pubkey_hash` since pubkey hash matches `naddr`).
4. Feed this into `pingback_handshakes_try_finish`.
5. Assert: `PeerDB::get_peer(conn, network_id, &third_party_addrbytes, arbitrary_port)` returns `Some(neighbor)` with `neighbor.public_key == attacker_pubkey`, proving an unauthenticated, unverified write of a forged address/key pair into the frontier.

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

**File:** stackslib/src/net/neighbors/walk.rs (L1713-1724)
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
```

**File:** stackslib/src/net/neighbors/db.rs (L378-408)
```rust
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
```

**File:** stackslib/src/net/chat.rs (L481-488)
```rust
        let addr = NeighborKey::from_handshake(peer_version, network_id, handshake_data);
        let pubk = handshake_data
            .node_public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;

        let peer_opt = PeerDB::get_peer(conn, network_id, &addr.addrbytes, addr.port)
            .map_err(net_error::DBError)?;
```

**File:** stackslib/src/net/chat.rs (L1275-1286)
```rust
        if updated && self.stats.outbound {
            // save the new key
            let tx = network.peerdb_tx_begin().map_err(net_error::DBError)?;
            let (mut neighbor, _) = Neighbor::load_and_update(
                &tx,
                message.preamble.peer_version,
                message.preamble.network_id,
                &handshake_data,
            )?;
            neighbor.save_update(&tx, None)?;
            tx.commit()
                .map_err(|e| net_error::DBError(db_error::SqliteError(e)))?;
```

**File:** stackslib/src/net/neighbors/neighbor.rs (L67-74)
```rust
    pub fn save(
        &mut self,
        tx: &DBTx<'_>,
        stacker_dbs: Option<&[QualifiedContractIdentifier]>,
    ) -> Result<bool, net_error> {
        self.last_contact_time = get_epoch_time_secs();
        PeerDB::try_insert_peer(tx, self, stacker_dbs.unwrap_or(&[])).map_err(net_error::DBError)
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
