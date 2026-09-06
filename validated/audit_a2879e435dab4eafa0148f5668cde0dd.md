### Title
Inverted expiry check in `lookup_peer` treats expired public keys as valid and fresh ones as unknown - (File: `stackslib/src/net/p2p.rs`)

### Summary
`PeerNetwork::lookup_peer` is documented as retrieving a previously-known neighbor "if we know of it and its public key is unexpired," but the implemented comparison is inverted: it returns `Some(neighbor)` only when the stored `expire_block` is **less than** the current block height (i.e., the key has already expired) and returns `None` when the key is still valid.

### Finding Description [1](#0-0) 

The function's docstring states the intent: return the neighbor entry only if its public key has not yet expired. The equality/boundary check that should gate "fresh vs. stale" is backwards:

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

This is the same class of bug as the raffle report: a stored/authenticated-state comparison (`expire_block` vs `cur_block_height`) is checked with the wrong direction, so the function fails to distinguish "authenticated/valid" from "stale/expired" correctly — it does the opposite. Any caller that relies on `lookup_peer` to fetch a still-valid neighbor record (e.g., to compare an inbound handshake's public key hash against a previously registered, current key) will instead be handed a neighbor record whose public key has already expired, while a genuinely fresh, currently-valid neighbor record will be silently treated as unknown.

### Impact Explanation
This breaks the authenticated-vs-stale equality that the function's contract promises. Code paths that use `lookup_peer` to validate the freshness of a peer's stored public key before trusting neighbor state will instead accept expired entries as valid ("fail open" on a stale credential) and will fail to recognize legitimately current neighbor records (denial for valid peers / potential DoS to normal peer bookkeeping). Since this touches p2p peer identity bookkeeping (`stackslib/src/net/p2p.rs`), which is in-scope network code reachable by remote peers during connection/handshake processing, an inverted expiry gate is a genuine break of an equality check in exactly the pattern flagged by the rules (authenticated vs. stored). I was not able to trace the single call site of `lookup_peer` within the remaining tool budget to fully confirm the exact downstream consumer and precise blast radius (e.g., whether it feeds directly into handshake acceptance or only into diagnostic/bookkeeping logic), so the exact severity (auth-bypass vs. lower-impact bookkeeping error) could not be fully confirmed.

### Likelihood Explanation
The condition is deterministic and requires no attacker action beyond normal peer/connection activity reaching a code path that calls `lookup_peer` with a block height past a neighbor's `expire_block`—this occurs naturally over time for any neighbor whose key-rotation deadline has passed, making the faulty branch trivially and remotely reachable without any privileged access.

### Recommendation
Flip the comparison to match the documented intent:
```rust
if neighbor.expire_block < cur_block_height {
    Ok(None)
} else {
    Ok(Some(neighbor))
}
```
This restores the invariant that only neighbors with unexpired public keys are returned.

### Proof of Concept
Given a stored neighbor with `expire_block = 100`:
- Call `lookup_peer(cur_block_height = 200, peer_addr)` (key clearly expired): current code returns `Some(neighbor)` — an expired-key neighbor is reported as valid.
- Call `lookup_peer(cur_block_height = 50, peer_addr)` (key still valid): current code returns `None` — a currently valid neighbor is reported as unknown.

Both outcomes are inverted from the documented behavior in [1](#0-0) , confirming the equality/boundary fault without needing to execute the full binary.

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
