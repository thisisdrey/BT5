### Title
Inverted expiry check in `PeerNetwork::lookup_peer` causes expired peer public keys to be treated as valid (and vice versa) - (File: stackslib/src/net/p2p.rs)

### Summary
`PeerNetwork::lookup_peer` is documented and intended to "Get the neighbor if we know of it and its public key is unexpired," but its comparison is inverted: it returns `Some(neighbor)` when the key **is** expired and `None` when the key is **not** expired.

### Finding Description
`lookup_peer` is called from `register_peer` for every newly-accepted inbound TCP socket, using the peer's socket address to look up a previously-known `Neighbor` record and pre-seed the new `ConversationP2P` with that neighbor's stored public key: [1](#0-0) 

```
/// Get the neighbor if we know of it and it's public key is unexpired.
fn lookup_peer(...) -> Result<Option<Neighbor>, net_error> {
    ...
    match neighbor_opt {
        None => Ok(None),
        Some(neighbor) => {
            if neighbor.expire_block < cur_block_height {
                Ok(Some(neighbor))     // <-- key IS expired, yet treated as present/valid
            } else {
                Ok(None)               // <-- key is NOT expired, yet treated as unknown
            }
        }
    }
}
```

Compare this to the equivalent, correctly-implemented check elsewhere in the same codebase, `Neighbor::load_by_address`, which treats `expire_block < block_height` as "expired ⇒ unknown": [2](#0-1) 

and `PeerNetwork::lookup_peer`'s sibling function used elsewhere for the same purpose (unused directly here but showing intended semantics is `expire_block < cur_block_height => expired`) is inverted only in `lookup_peer`.

The result of `lookup_peer` feeds directly into `register_peer`, which uses it to decide whether to pre-associate a known public key with the newly-connected socket: [3](#0-2) 

```
let neighbor_opt = match self.lookup_peer(self.chain_view.burn_block_height, &client_addr) { ... };
...
let (pubkey_opt, neighbor_key) = match neighbor_opt {
    Some(neighbor) => (Some(neighbor.public_key.clone()), neighbor.addr),
    None => (None, NeighborKey::from_socketaddr(...)),
};
...
new_convo.set_public_key(pubkey_opt);
```

Because of the inverted logic:
- When a stored neighbor's key has genuinely expired (`expire_block < cur_block_height`), `lookup_peer` still returns `Some(neighbor)`, so `register_peer` pre-seeds the new conversation's `public_key` with the **stale/expired** key.
- When a stored neighbor's key is still valid/fresh (not expired), `lookup_peer` returns `None`, so the conversation is treated as if the node is completely unknown (public key unset), discarding legitimate, still-valid key state.

This breaks the "authenticated vs. stored" equality that this reachable code path exists to enforce: the freshness gate for using a previously-learned public key to constrain/authenticate an inbound handshake is applied backwards.

### Impact Explanation
`ConversationP2P::get_public_key()`, which is set here, is used by `validate_handshake` to decide whether the incoming `Handshake` message must be signed by an already-known key (`Some(_)` branch, which enforces address/port consistency but not that the new key matches the old one) versus being treated as a brand-new, self-asserted key (`None` branch, which only checks the message is self-consistently signed by whatever key it claims). Because `lookup_peer`'s expiry check is inverted, a stale/expired public key on record for a given `(addr, port)` gets fed forward into `new_convo` as if it were still valid/known, and a currently-valid one gets dropped as unknown. This is a real correctness defect in the network-identity bootstrap for every inbound connection (`register_peer` is called for all newly accepted sockets), degrading the peer-authentication invariants around key rotation/expiry that the rest of the peer database logic (`Neighbor::load_by_address`, `chat.rs::validate_handshake`) correctly implements.

That said, I could not fully trace, within the available context, an end-to-end concrete exploit chain showing this inversion alone allows a remote unauthenticated party to force a specific message to be accepted as signed by a key it does not control, or to bypass `validate_handshake`'s own independent `expire_block_height <= chain_view.burn_block_height` check on the handshake payload itself (which is a separate, correctly-implemented gate in `chat.rs`). The bug is a confirmed logic inversion with a clear discoverable root cause and a reachable, unauthenticated-facing code path (every inbound connection), but without being able to fully explore `connection.rs`'s use of the pre-seeded public key (the fetch was cut off), I cannot certify that it independently produces one of the required Critical/High impacts (e.g., outright auth bypass) versus being a defense-in-depth/robustness regression that is otherwise masked by `validate_handshake`'s separate expiry check on the handshake data itself.

### Likelihood Explanation
Triggering the code path only requires establishing an ordinary inbound TCP connection to a Stacks node that has a stale `Neighbor` record on file for that connecting `(IP, port)`, which is routine and requires no special privileges — every accepted inbound connection calls `register_peer` → `lookup_peer`. However, exploiting the resulting state to achieve a certified Critical/High impact (as opposed to merely a robustness bug) would additionally require an attacker capable of controlling or spoofing the connecting address that matches a stale record, which is a mitigating factor.

### Recommendation
Fix the comparison in `lookup_peer` to match its own doc comment and the correct pattern used in `Neighbor::load_by_address`:
```rust
if neighbor.expire_block < cur_block_height {
    Ok(None)          // expired -> treat as unknown
} else {
    Ok(Some(neighbor)) // still valid -> return it
}
```

### Proof of Concept
Not independently reproducible from the indexed context alone: I was unable to retrieve `stackslib/src/net/connection.rs`'s `set_public_key`/`get_public_key` implementation in full (index truncation on the final iteration), which is needed to demonstrate a concrete forged-signature or auth-bypass scenario end-to-end. Given the finding is a confirmed source-level logic inversion in unprivileged, remotely-reachable code (`register_peer`, invoked for every inbound socket), but I could not conclusively prove a standalone Critical/High-tier exploit distinct from `validate_handshake`'s separate expiry check, I recommend a Devin/engineer session with full repository access to trace `ConversationP2P::set_public_key`/`get_public_key` usage in `connection.rs` and `chat.rs::validate_handshake`'s `Some(_)` branch to confirm whether this inversion alone permits accepting a handshake as though it came from a previously-known (but actually expired) key, or otherwise weakens peer authentication guarantees, before finalizing severity.

### Citations

**File:** stackslib/src/net/p2p.rs (L1798-1824)
```rust
    /// Get the neighbor if we know of it and it's public key is unexpired.
    fn lookup_peer(
        &self,
        cur_block_height: u64,
        peer_addr: &SocketAddr,
    ) -> Result<Option<Neighbor>, net_error> {
        let conn = self.peerdb.conn();
        let addrbytes = PeerAddress::from_socketaddr(peer_addr);
        let neighbor_opt = PeerDB::get_peer(
            conn,
            self.local_peer.network_id,
            &addrbytes,
            peer_addr.port(),
        )
        .map_err(net_error::DBError)?;

        match neighbor_opt {
            None => Ok(None),
            Some(neighbor) => {
                if neighbor.expire_block < cur_block_height {
                    Ok(Some(neighbor))
                } else {
                    Ok(None)
                }
            }
        }
    }
```

**File:** stackslib/src/net/p2p.rs (L1997-2021)
```rust
        let neighbor_opt = match self.lookup_peer(self.chain_view.burn_block_height, &client_addr) {
            Ok(neighbor_opt) => neighbor_opt,
            Err(e) => {
                debug!("Failed to look up peer {}: {:?}", client_addr, &e);
                self.deregister_socket(event_id, socket);
                return Err(e);
            }
        };

        // NOTE: the neighbor_key will have the same network_id as the remote peer, and the same
        // major version number in the peer_version.  The chat logic won't accept any messages for
        // which this is not true.  Comparison and Hashing are defined for neighbor keys
        // appropriately, so it's okay for us to use self.peer_version and
        // self.local_peer.network_id here for the remote peer's neighbor key.
        let (pubkey_opt, neighbor_key) = match neighbor_opt {
            Some(neighbor) => (Some(neighbor.public_key.clone()), neighbor.addr),
            None => (
                None,
                NeighborKey::from_socketaddr(
                    self.peer_version,
                    self.local_peer.network_id,
                    &client_addr,
                ),
            ),
        };
```

**File:** stackslib/src/net/neighbors/neighbor.rs (L94-112)
```rust
        match peer_opt {
            None => {
                Ok(None) // unkonwn
            }
            Some(peer) => {
                // expired public key?
                if peer.expire_block < block_height {
                    Ok(None)
                } else {
                    let pubkey_160 = Hash160::from_node_public_key(&peer.public_key);
                    if pubkey_160 == neighbor_address.public_key_hash {
                        // we know this neighbor's key
                        Ok(Some(peer))
                    } else {
                        // this neighbor's key may be stale
                        Ok(None)
                    }
                }
            }
```
