### Title
`PeerNetwork::lookup_peer` treats expired neighbor public keys as valid, pre-authenticating stale keys on new connections - (File: stackslib/src/net/p2p.rs)

### Summary
`PeerNetwork::lookup_peer` in `stackslib/src/net/p2p.rs` has an inverted expiration check relative to every other equivalent lookup in the codebase (e.g. `Neighbor::load_by_address`), causing it to return the stored `Neighbor` (and thus its public key) exactly when that neighbor's key has *expired*, and to return `None` (unknown) when the key is still *valid*.

### Finding Description
`lookup_peer` is documented as "Get the neighbor if we know of it and it's public key is unexpired": [1](#0-0) 

```rust
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
```

The condition is backwards: `neighbor.expire_block < cur_block_height` means the neighbor's key **has already expired** (its expiry block is in the past), yet this branch returns `Some(neighbor)`, treating the peer as known/valid. Conversely, when the key is still valid (`expire_block >= cur_block_height`), the function returns `None`, treating the peer as unknown.

Compare this to the correct analog in the same crate, `Neighbor::load_by_address`, which does the opposite (correct) check — expired keys yield `None`: [2](#0-1) 

`lookup_peer`'s result feeds directly into `register_peer`, which uses it to pre-seed the newly-created `ConversationP2P`'s public key before any handshake occurs: [3](#0-2) 

```rust
let neighbor_opt = match self.lookup_peer(self.chain_view.burn_block_height, &client_addr) { ... };
...
let (pubkey_opt, neighbor_key) = match neighbor_opt {
    Some(neighbor) => (Some(neighbor.public_key.clone()), neighbor.addr),
    None => (None, NeighborKey::from_socketaddr(...)),
};
...
new_convo.set_public_key(pubkey_opt);
```

Because of the inverted check, a fresh inbound TCP connection from an address whose peer-DB entry has a **valid, unexpired** key gets `pubkey_opt = None` (i.e. treated as unknown, forcing a full unauthenticated handshake — the normally-correct path but for the wrong reason). Meanwhile, a connection from an address whose stored key has **already expired** is pre-populated with that stale public key via `set_public_key`. `validate_handshake` in `chat.rs` branches on whether `self.connection.get_public_key()` is `Some` or `None`: [4](#0-3) 

When a key is already set (the expired-key case triggered by this bug), the handshake path skips the `verify_secp256k1` check against the freshly presented `node_public_key` in the `None` branch and instead only checks the outbound address-consistency rule — it does not re-verify that the incoming handshake message itself is signed by the pre-populated (stale) key at that point. This means a remote peer that reconnects from an address previously associated with an expired key benefits from the node believing it already has an authenticated key for that address, weakening the intended "unauthenticated until proven via signed handshake" invariant that the rest of the authentication logic assumes.

### Impact Explanation
This breaks the authenticated-vs-stored equality that `register_peer`/`lookup_peer` is supposed to enforce: expired keys (which should never be treated as trusted) are the *only* case where a stale public key is injected into a fresh conversation state prior to any signature verification. This is a real logic inversion reachable by any unprivileged remote peer that connects to a node's P2P port, and it directly affects how the handshake validation branches, undermining assumptions in `validate_handshake`. It does not appear to allow a full authentication bypass in the read of the code (since a subsequent handshake message still needs `pubk` recomputation in `update_from_handshake_data`), but it does allow a key held by an attacker whose entry had actually expired to have its conversation pre-seeded as though the key were still authenticated, which is a "serving non-canonical/expired state as canonical" class defect at the node's neighbor-registration gate.

### Likelihood Explanation
High likelihood of being reachable: `lookup_peer` is invoked on every single inbound TCP connection registration (`register_peer`), which is triggered by any unprivileged remote peer connecting to the P2P listener. No special privileges, keys, or timing are required beyond having (or having previously had) a peer-DB entry with an expired `expire_block`, which happens naturally for any peer whose earlier handshake public key lapsed.

### Recommendation
Fix the condition in `lookup_peer` to match `Neighbor::load_by_address` and the function's own doc comment — return `Some(neighbor)` only when `neighbor.expire_block >= cur_block_height` (i.e., not yet expired), and `None` otherwise:
```rust
Some(neighbor) => {
    if neighbor.expire_block >= cur_block_height {
        Ok(Some(neighbor))
    } else {
        Ok(None)
    }
}
```

### Proof of Concept
1. A remote peer performs a handshake with the node and its resulting `Neighbor` entry is stored in `PeerDB` with some `expire_block`.
2. Time/burn-height advances past `expire_block`, so the key is now expired.
3. The same remote address reconnects (new TCP connection, new `event_id`).
4. `register_peer` calls `lookup_peer(cur_block_height, client_addr)`, which (due to the inverted check) returns `Some(neighbor)` because `expire_block < cur_block_height` is true.
5. `register_peer` then calls `new_convo.set_public_key(Some(neighbor.public_key))`, pre-seeding the new conversation's authenticated public key with the expired key.
6. Subsequent handshake processing in `validate_handshake` takes the `Some(_)` branch instead of `None`, skipping the initial `verify_secp256k1` check that would normally be performed for a peer with no known key — deviating from the intended per-connection authentication flow for what should be treated as an unknown/expired peer. [5](#0-4) [6](#0-5)

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

**File:** stackslib/src/net/chat.rs (L1058-1092)
```rust
        match self.connection.get_public_key() {
            None => {
                // if we don't yet have a public key for this node, verify the message.
                // if it's improperly signed, it's probably a poorly-timed re-key request (but either way the message should be rejected)
                message
                    .verify_secp256k1(&handshake_data.node_public_key)
                    .map_err(|_e| {
                        debug!(
                            "{:?}: invalid handshake: not signed with given public key",
                            &self
                        );
                        net_error::InvalidMessage
                    })?;
            }
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
        };
```
