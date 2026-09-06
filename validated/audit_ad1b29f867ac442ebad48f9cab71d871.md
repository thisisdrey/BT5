### Title
Unauthenticated peers can inject an arbitrary self-reported IP/port into the neighbor gossip network via inbound Handshake - ([File: stackslib/src/net/chat.rs])

### Summary
`ConversationP2P::validate_handshake` only cross-checks a peer's self-reported `HandshakeData.addrbytes/port` against the actual TCP peer address for **outbound** connections. For **inbound** connections — the only direction an unauthenticated remote party actually controls — no such check exists. The unverified, attacker-supplied address is then persisted into the local `PeerDB` as this peer's `Neighbor` record and is subsequently served to other nodes via `GetNeighbors`/`Neighbors` gossip, letting a remote unauthenticated party spoof its network address to the rest of the peer graph.

### Finding Description
`validate_handshake` in `stackslib/src/net/chat.rs:1047-1127` explicitly documents and implements the asymmetry: [1](#0-0) 

```
Some(_) => {
    // for outbound connections, the self-reported address must match socket address...
    if self.stats.outbound
        && (!handshake_data.addrbytes.is_anynet()
            && (self.peer_addrbytes != handshake_data.addrbytes
                || self.peer_port != handshake_data.port))
    { ... return Err(net_error::InvalidHandshake); }
}
```

For inbound connections (`self.stats.outbound == false`) this branch is skipped entirely — the comment even states the real peer socket address "may not be the same as the address the remote peer thinks it has," so the check is deliberately not enforced for inbound handshakes. This is the closest available cross-check between "verified" (observed TCP source) and "claimed" (self-reported handshake field) — analogous to the sshpiper bug, which trusted a claimed source address (via unauthenticated proxy-protocol header) without validating it against the actual observed connection.

Once `handle_handshake` calls `validate_handshake` successfully, it unconditionally copies the unverified fields into connection state via `update_from_handshake_data`: [2](#0-1)  — setting `self.handshake_addrbytes`/`self.handshake_port` directly from `handshake_data`, with no socket-address cross-check for inbound peers.

This self-reported `NeighborAddress` (built from `handshake_addrbytes`/`handshake_port` via `to_handshake_neighbor_address`, [3](#0-2) ) then flows into `Neighbor::load_and_update` and is written to the PeerDB via `add_or_schedule_replace_neighbor`: [4](#0-3) . From there, standard neighbor-walk/gossip logic serves these `Neighbor` records to other peers who query `GetNeighbors`, propagating the forged address across the network — the same "forged gossip relayed" pattern called out as in-scope.

### Impact Explanation
An unauthenticated remote peer performing an inbound Handshake can claim any `addrbytes`/`port` it wants; the node accepts it without validation, stores it in its `PeerDB`, and later gossips it to its other neighbors as if it were a verified network address for that peer. This lets an attacker: (1) pollute a victim node's peer table with fabricated addresses (e.g., pointing to a third party's IP to cause other honest nodes to connect to/flood that party), and (2) degrade the peer-discovery graph's integrity network-wide, since every peer that receives the poisoned `Neighbors` reply will attempt to reach the forged address. This matches the Critical bucket criterion "network-wide propagation of forged data."

### Likelihood Explanation
High likelihood: any remote, unauthenticated party can simply initiate an inbound TCP connection and send a `Handshake` message with an arbitrary `addrbytes`/`port` field — no special privileges, valid key ownership of a target address, or race condition is required. The check that would prevent this (`self.stats.outbound` gate in `validate_handshake`) is present in the code but structurally excludes the inbound case that attackers control.

### Recommendation
For inbound connections, do not blindly trust `handshake_data.addrbytes/port`; either drop the self-reported address entirely and rely solely on the observed socket address (as inbound peer identity/liveness already uses `self.peer_addrbytes`/`self.peer_port`), or mark such self-reported addresses as "unconfirmed" until independently corroborated (e.g., via a successful NAT-punch/pingback handshake as already implemented for `PingbackHandshakesFinish`), and never propagate unconfirmed self-reported addresses in `GetNeighbors` responses.

### Proof of Concept
1. Attacker opens an inbound TCP connection to a victim Stacks node.
2. Attacker sends a validly-signed `Handshake` message (`StacksMessageType::Handshake`) with `HandshakeData.addrbytes` = victim-of-choice's IP and an arbitrary port.
3. `validate_handshake` (stackslib/src/net/chat.rs:1047) skips the address-match check because `self.stats.outbound == false`.
4. `handle_handshake` → `update_from_handshake_data` stores the forged address in `self.handshake_addrbytes/handshake_port` (chat.rs:1145-1146).
5. `add_or_schedule_replace_neighbor` (stackslib/src/net/neighbors/db.rs:368) persists a `Neighbor` entry using this forged address.
6. A third node later sends `GetNeighbors` to the victim node and receives the forged `NeighborAddress` in the reply, propagating the spoofed address across the network.

### Citations

**File:** stackslib/src/net/chat.rs (L635-647)
```rust
    pub fn to_handshake_neighbor_address(&self) -> NeighborAddress {
        let pubkh = if let Some(pubk) = self.ref_public_key() {
            Hash160::from_node_public_key(pubk)
        } else {
            Hash160([0u8; 20])
        };

        NeighborAddress {
            addrbytes: self.handshake_addrbytes.clone(),
            port: self.handshake_port,
            public_key_hash: pubkh,
        }
    }
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

**File:** stackslib/src/net/chat.rs (L1141-1147)
```rust
        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();
```

**File:** stackslib/src/net/neighbors/db.rs (L368-408)
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
```
