Found a strong analog: an inverted staleness check in `lookup_peer`.### Title
Inverted peer public-key expiry check in `lookup_peer` treats stale (expired) peer records as valid and rejects fresh ones - (File: `stackslib/src/net/p2p.rs`)

### Summary
`PeerNetwork::lookup_peer` is documented to "Get the neighbor if we know of it and its public key is unexpired," but its conditional is inverted: it returns `Some(neighbor)` only when the neighbor's `expire_block` is *less than* the current burn block height (i.e. already expired), and returns `None` when the key is still valid (not expired). This breaks the intended equality "stale (expired) vs. fresh (valid) public key," causing an expired/stale peer identity to be treated as current, while a fresh, still-valid one is treated as unknown.

### Finding Description
`lookup_peer` loads a `Neighbor` record from the `PeerDB` by socket address and is meant to only surface it when the stored public key has not yet expired: [1](#0-0) 

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

The comparison is backwards relative to every other expiry check in the codebase. Elsewhere, the convention is consistently "expired" == `expire_block < current_height`, and such entries are treated as **invalid**/unknown — e.g. `Neighbor::load_by_address` explicitly returns `None` when `peer.expire_block < block_height` [2](#0-1) , and the handshake validator rejects a peer whose `expire_block_height <= chain_view.burn_block_height` as "stale public key (expired)" [3](#0-2) .

`lookup_peer` inverts this: it hands back the neighbor record precisely when it is expired, and suppresses it precisely when it is still valid. This is the same class of fault as the reported Chainlink issue — an equality/threshold meant to gate "fresh vs. stale" data is implemented backwards, so stale data is accepted as if canonical.

### Impact Explanation
Any code path that relies on `lookup_peer` to decide whether a socket address is associated with a still-valid, previously-authenticated public key will now do the opposite of what its contract promises: it will report "known and current" for a peer whose session key has already expired (and could plausibly have been rotated/compromised or reused by another party at that address), and it will report "unknown" for a peer whose key is genuinely current and trustworthy. This is a case of serving non-canonical (expired) identity state as canonical — matching the "High: serving non-canonical state as canonical" impact bucket for this analysis. I was not able to fully trace every call site of `lookup_peer` (grep found only its definition and one additional reference I could not inspect before running out of tool calls), so I cannot state with certainty which specific downstream decision (e.g., accepting an inbound connection without a fresh handshake, or bypassing re-authentication) is affected in this snapshot of the code. This should be verified against the call site before treating this as a confirmed remotely-exploitable bypass.

### Likelihood Explanation
The condition is remotely triggerable by any peer whose previously-recorded public key entry has naturally expired (which happens routinely via the node's own key-rotation/expiry mechanism), so no privileged access or secret material is required to hit the inverted branch — an attacker only needs to reconnect from an address/port pair that has a stale `PeerDB` entry.

### Recommendation
Fix the comparison to match the codebase's established convention:
```rust
if neighbor.expire_block < cur_block_height {
    Ok(None) // expired -- not valid
} else {
    Ok(Some(neighbor)) // still valid
}
```
and audit the call site(s) of `lookup_peer` to confirm no logic elsewhere compensates for (or depends on) the inverted behavior.

### Proof of Concept
1. Populate a `PeerDB` entry for `peer_addr` with `expire_block = H0`.
2. Advance `cur_block_height` past `H0` (i.e., the key is now expired per every other check in the codebase).
3. Call `PeerNetwork::lookup_peer(cur_block_height, peer_addr)`.
4. Observe it returns `Ok(Some(neighbor))` — the stale record is treated as valid — contradicting the function's documented contract and the expiry semantics used everywhere else (`Neighbor::load_by_address`, `validate_handshake`).

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

**File:** stackslib/src/net/neighbors/neighbor.rs (L98-102)
```rust
            Some(peer) => {
                // expired public key?
                if peer.expire_block < block_height {
                    Ok(None)
                } else {
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
