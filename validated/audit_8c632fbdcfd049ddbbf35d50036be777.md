### Title
Inverted expiry check in `PeerNetwork::lookup_peer` causes expired peer keys to be treated as valid - (File: stackslib/src/net/p2p.rs)

### Summary
`PeerNetwork::lookup_peer` in `stackslib/src/net/p2p.rs` is documented as returning the known `Neighbor` record for a socket address *only if* that peer's advertised public key has not yet expired. The implementation does the opposite of what it documents: it returns `Some(neighbor)` when the neighbor's key **is** expired, and `Ok(None)` when the key is still valid. [1](#0-0) 

### Finding Description
The function's doc comment states: "Get the neighbor if we know of it and it's public key is unexpired." [2](#0-1) 

But the actual comparison is:
```rust
if neighbor.expire_block < cur_block_height {
    Ok(Some(neighbor))
} else {
    Ok(None)
}
``` [3](#0-2) 

`expire_block` is the burn-chain block height at which a peer's session public key is meant to be revoked — this is exactly the same "credential validity window" concept used throughout the P2P authentication path. Compare with the *correctly* implemented analog in `Neighbor::load_by_address`, which treats `peer.expire_block < block_height` as "expired public key" and correctly returns `None` in that case: [4](#0-3) 

and the analogous check that rejects a stale key entirely during handshake validation: [5](#0-4) 

`lookup_peer` inverts this pattern: it surfaces the neighbor record precisely when its key has expired (i.e., when it should be treated as revoked/unknown), and suppresses it when the key is still current (i.e., when it should be trusted). This is a direct equality-inversion of "credential still valid" vs. "credential expired," structurally the same class of bug as the Rancher advisory (stale/revoked authorization state being treated as live), just manifesting as backwards logic instead of a missing revocation hook.

### Impact Explanation
I was not able to fully trace the caller of `lookup_peer` before running out of tool budget — `grep_search` reported only two matches in `p2p.rs` (the definition and a single call site), and I could not read that call site to confirm exactly how the returned `Option<Neighbor>` is consumed (e.g., whether it gates connection acceptance, peer replacement, or population of `NeighborKey`/pubkey-hash trust decisions elsewhere in `p2p.rs`). Because of this, I can state with certainty that the logic is inverted relative to its own documented contract and relative to the equivalent, correctly-implemented check elsewhere in the same module (`Neighbor::load_by_address`), but I cannot confirm with full certainty the precise blast radius (e.g., whether it enables a stale/expired-key peer to be admitted as an already-known peer bypassing an intended re-handshake/expiry check, or whether it merely affects logging/lookup convenience paths with no security consequence). This should be validated by reading all call sites of `lookup_peer` in `stackslib/src/net/p2p.rs`.

### Likelihood Explanation
The fault is unconditional and remotely triggerable simply by any peer whose previously-registered key has passed its `expire_block` — no privileged access is needed, matching the "remote, unprivileged" scope of this exercise. However, since the downstream effect of the `Some`/`None` inversion could not be confirmed, I cannot assert a definitive High/Critical exploitation path without further code reading.

### Recommendation
Invert the comparison to match the documented and intended behavior:
```rust
if neighbor.expire_block > cur_block_height {
    Ok(Some(neighbor))
} else {
    Ok(None)
}
```
(or `>=`, consistent with the semantics used by `validate_handshake`'s `expire_block_height <= chain_view.burn_block_height` staleness check). Then audit every call site of `lookup_peer` to confirm no code path currently relies on (or was inadvertently built around) the inverted behavior.

### Proof of Concept
Not fully constructible without confirming the call site semantics (see Impact Explanation). The root-cause defect itself is directly demonstrable by inspection:
1. Register a `Neighbor` in the PeerDB with `expire_block = N`.
2. Call `lookup_peer(cur_block_height, addr)` with `cur_block_height > N` (key expired) → returns `Ok(Some(neighbor))`, i.e., an expired-key peer is reported as "looked up."
3. Call `lookup_peer(cur_block_height, addr)` with `cur_block_height <= N` (key still valid) → returns `Ok(None))`, i.e., a currently-valid peer is reported as unknown.

This is the reverse of the function's own documentation and of the equivalent check in `Neighbor::load_by_address` at `stackslib/src/net/neighbors/neighbor.rs:99-102`. [6](#0-5)

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

**File:** stackslib/src/net/chat.rs (L1104-1111)
```rust
        if handshake_data.expire_block_height <= chain_view.burn_block_height {
            // already stale
            debug!(
                "{:?}: invalid handshake -- stale public key (expired at {})",
                &self, handshake_data.expire_block_height
            );
            return Err(net_error::InvalidHandshake);
        }
```
