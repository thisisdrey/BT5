### Title
Expired peer public keys are treated as valid due to an inverted expiration check in `PeerNetwork::lookup_peer` - (File: `stackslib/src/net/p2p.rs`)

### Summary
`PeerNetwork::lookup_peer` in `stackslib/src/net/p2p.rs` is documented to "Get the neighbor if we know of it and it's public key is unexpired," but its implementation inverts the comparison: it returns `Some(neighbor)` when the neighbor's key has *already expired* and returns `None` when the key is still valid. This is the exact analog of the reported bug class — a credential that should be invalidated once its validity window (`expire_block`) has passed instead continues to be honored, while currently-valid credentials are incorrectly discarded.

### Finding Description
The correct pattern for checking a neighbor's key expiration exists elsewhere in the codebase, in `Neighbor::load_by_address`: [1](#0-0) 

```
// expired public key?
if peer.expire_block < block_height {
    Ok(None)
} else {
    let pubkey_160 = Hash160::from_node_public_key(&peer.public_key);
    ...
}
```

Here, `expire_block < block_height` correctly means the key has expired, and the function returns `None` (i.e., "we don't have a trustworthy record of this neighbor").

Contrast this with `PeerNetwork::lookup_peer`: [2](#0-1) 

```rust
/// Get the neighbor if we know of it and it's public key is unexpired.
fn lookup_peer(
    &self,
    cur_block_height: u64,
    peer_addr: &SocketAddr,
) -> Result<Option<Neighbor>, net_error> {
    ...
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

This function returns the stored `Neighbor` record (treating it as valid/known) precisely when `expire_block < cur_block_height` — i.e., precisely when the key **is** expired — and discards the record (`None`) when the key is still within its valid window. This is the opposite of both the function's own doc comment and the correct logic used in `Neighbor::load_by_address` for the identical check.

This breaks the equality/validity boundary that the "expire_block" field is meant to enforce: a neighbor whose session public key has expired should no longer be recognized as an authenticated/known entity at that address, exactly as in the Merit Circle bug where a lock that has expired should stop being treated as an active, high-value lock. Here, an expired P2P session key is instead the *only* case treated as "known", while a currently valid key is treated as "unknown."

### Impact Explanation
`lookup_peer` is intended to answer "do we already have a trusted, unexpired record of the peer at this address?" With the inverted logic, any caller relying on this function will:
- Treat a peer with a **stale/expired** public key as still being the known, previously-authenticated neighbor at that address.
- Treat a peer with a **currently valid** public key as unknown.

This undermines the freshness/expiration invariant that `expire_block` is designed to enforce for P2P peer identity (the same invariant `Neighbor::load_by_address` correctly implements). Depending on how the caller uses this result (e.g., to decide whether to skip a fresh handshake/authentication step, apply allow-list logic, or bypass a full re-validation), this can let an attacker who has captured an old/expired session key continue to be recognized as a previously-trusted peer, or can cause valid peers to be wrongly rejected as unrecognized, degrading trust decisions made by the P2P layer.

### Likelihood Explanation
This is a deterministic logic bug (inverted comparison), not a probabilistic or resource-intensive condition — any caller of `lookup_peer` after a peer's `expire_block` has passed will exhibit the inverted (incorrect) behavior on every invocation. No unusual network conditions or attacker sophistication are required beyond controlling the address/session-key pairing timing.

### Recommendation
Fix the comparison to match the documented intent and the correct implementation used in `Neighbor::load_by_address`:
```rust
if neighbor.expire_block < cur_block_height {
    Ok(None)      // expired -> not known/trusted
} else {
    Ok(Some(neighbor))  // still valid -> known/trusted
}
```
Add or update a unit test asserting that `lookup_peer` returns `None` for a neighbor whose `expire_block` is in the past, and `Some(..)` for one still within its validity window, mirroring the existing test coverage for `Neighbor::load_by_address`.

### Proof of Concept
1. Insert a `Neighbor` record for address `A` into the peer DB with `expire_block = 100`.
2. Call `PeerNetwork::lookup_peer(cur_block_height = 200, peer_addr = A)` (i.e., after expiration).
3. Observe that the function returns `Ok(Some(neighbor))` instead of `Ok(None)`, confirming that an expired peer record is treated as valid.
4. Conversely, call `lookup_peer(cur_block_height = 50, peer_addr = A)` (before expiration) and observe it incorrectly returns `Ok(None)`, confirming a currently valid peer record is treated as unknown.

Note: I was not able to fully enumerate every call site of `lookup_peer` within the allotted investigation (grep showed only its definition and one other reference in `p2p.rs`, which I did not have the opportunity to inspect before running out of tool calls), so the precise downstream behavioral consequence at the call site is not fully confirmed. The root-cause logic inversion itself, however, is clearly established by direct comparison with the doc comment and the correct sibling implementation in `neighbor.rs`.

### Citations

**File:** stackslib/src/net/neighbors/neighbor.rs (L98-111)
```rust
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
```

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
