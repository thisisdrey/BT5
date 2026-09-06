### Title
`PeerNetwork::lookup_peer` returns an expired neighbor's identity as if it were a live, unexpired peer, inverting the intended freshness check - (File: stackslib/src/net/p2p.rs)

### Summary
`PeerNetwork::lookup_peer` is documented as "Get the neighbor if we know of it and its public key is unexpired," but the boolean condition it uses to decide whether to return `Some(neighbor)` is inverted relative to that documented contract and relative to the equivalent, correctly-implemented check elsewhere in the same crate.

### Finding Description
`lookup_peer` in `stackslib/src/net/p2p.rs` looks up a peer's DB record and is supposed to return it only if its `expire_block` has not yet passed: [1](#0-0) 

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

`neighbor.expire_block < cur_block_height` is true precisely when the neighbor's key has *already expired*. As written, the function returns `Some(neighbor)` (i.e., "found, and usable") only in the expired case, and returns `None` (i.e., "not found / not usable") when the key is still valid — the exact opposite of the doc comment's contract.

This is directly comparable to the equality-direction defect in the external report: two code paths in the same codebase implement the same boundary check with opposite polarity. The sibling function `Neighbor::load_by_address` in `stackslib/src/net/neighbors/neighbor.rs` implements the correct, intended semantics for the identical boundary condition: [2](#0-1) 

```rust
Some(peer) => {
    // expired public key?
    if peer.expire_block < block_height {
        Ok(None)
    } else {
        let pubkey_160 = Hash160::from_node_public_key(&peer.public_key);
        if pubkey_160 == neighbor_address.public_key_hash {
            Ok(Some(peer))
        } else {
            Ok(None)
        }
    }
}
```

Here, `expire_block < block_height` correctly yields `None` (rejected) for an expired peer, and only proceeds to return `Some(peer)` when the key is still valid. `lookup_peer` inverts this same test.

### Impact Explanation
`lookup_peer` is private to `PeerNetwork` and I could only find two references to it inside `stackslib/src/net/p2p.rs` (both within the same file); I was not able to fully trace the call sites and their downstream consumers within the tool budget available, so I cannot conclusively state how the inverted result propagates (e.g., whether it feeds into ban/allow decisions, connection admission, or peer replacement logic). Given the doc comment's stated purpose ("get the neighbor if it is unexpired"), an inverted result would mean:
- Expired neighbor records (whose keys are no longer trusted) are treated as valid/known peers.
- Legitimately fresh, unexpired neighbor records are treated as unknown.

Depending on the call site, this could affect whether a peer with a stale/revoked public key is treated as already-known (bypassing checks meant to gate on key freshness), which aligns with an "auth-gate that fails open" class of issue. However, since I could not verify the exact call sites' logic and their security consequences with certainty in the time available, I present this as a probable logic defect rather than a confirmed critical/high exploit path.

### Likelihood Explanation
This code path executes on ordinary peer lookups keyed by socket address and block height — no special network position or privileged access is required to trigger the lookup (any inbound/outbound connection attempt from an address that matches a stale, expired DB record would exercise it). The likelihood of the code being exercised is high; the likelihood of it constituting a *serious* remote impact depends on the (unverified) call sites.

### Recommendation
Fix the condition in `lookup_peer` to match its documented contract and the equivalent logic in `Neighbor::load_by_address`:
```rust
if neighbor.expire_block < cur_block_height {
    Ok(None)
} else {
    Ok(Some(neighbor))
}
```
Then audit all call sites of `lookup_peer` to confirm downstream logic was written assuming the (buggy) inverted semantics — if so, those call sites must be updated in tandem to avoid simply moving the bug elsewhere.

### Proof of Concept
Not fully verified: I confirmed the inverted condition by direct code inspection and by diffing it against the semantically identical check in `Neighbor::load_by_address`, but I was unable to locate and trace all call sites of `lookup_peer` within the available tool budget to construct a concrete end-to-end remote trigger/observable-effect scenario. This should be validated by a full trace of `lookup_peer`'s callers (both found in `stackslib/src/net/p2p.rs`) before treating this as a confirmed exploitable vulnerability rather than a logic defect.

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
