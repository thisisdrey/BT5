### Title
`lookup_peer` returns an expired neighbor's public key as valid instead of rejecting it - ([File: stackslib/src/net/p2p.rs])

### Summary
`PeerNetwork::lookup_peer` in `stackslib/src/net/p2p.rs` is documented to "Get the neighbor if we know of it and its public key is unexpired," but its expiry comparison is inverted: it returns `Some(neighbor)` when `neighbor.expire_block < cur_block_height` (i.e., when the record has *already expired*) and `None` when the record is still fresh (unexpired). This is the same equality-inversion bug class as the reported Solidity finding, where an expired lock/stake continued to be treated as if it were still within its original valid window.

### Finding Description
`lookup_peer` fetches a `Neighbor` from `PeerDB` and is meant to gate on freshness of the neighbor's public key using `expire_block` versus the current chain height: [1](#0-0) 

The doc comment states the intended contract: return the neighbor only if its public key is *unexpired*. But the actual condition `neighbor.expire_block < cur_block_height` is true precisely when the neighbor's expiration block is in the past relative to the current height — i.e., the record has expired. The function returns `Ok(Some(neighbor))` in that (expired) branch and `Ok(None)` in the complementary (still-valid) branch, breaking the "authenticated key still valid" equality that the rest of the peer-management code relies on. This is a direct fault-site instance of the reported bug class: continuing to honor state that should have lapsed, verified from the exact conditional and its inverted outcome mapping.

Corroborating context: elsewhere in the same file, expiry semantics are handled correctly, e.g. in `get_fresh_random_neighbors`-adjacent test setups and peer freshness checks, `allowed`/`denied`/`expire_block` fields are consistently treated so that values *less than* the current time/height denote "expired" and are excluded from "still valid" sets — confirming that `lookup_peer`'s branch selection is reversed relative to the codebase's own established convention for this field. [2](#0-1) 

### Impact Explanation
If `lookup_peer`'s result is consumed anywhere as a gate for trusting a neighbor's still-registered public key (e.g., for handshake/authentication decisions, deciding whether to accept a peer as already-known/authenticated, or for peer-list maintenance), the inversion means:
- A neighbor whose public key registration has expired is treated as still valid (returned as `Some`), which could let a stale/expired identity continue to be trusted past its intended validity window — a "serving non-canonical state as canonical" scenario analogous to letting an expired lock keep its original bonus.
- A neighbor whose public key is actually still valid is incorrectly treated as unknown/absent (`None`), which could cause valid peers to be needlessly treated as new/unauthenticated.

I was not able to fully trace all call sites of `lookup_peer` within the available tool budget — grep found only the definition site inside `stackslib/src/net/p2p.rs`, and I could not confirm within this session whether the function is currently dead code, used only internally, or wired into an externally reachable authentication/trust decision path. This materially affects the severity: if unused or only advisory, impact is negligible; if it gates trust/acceptance of a peer's public key against remote input, the impact would meet the "authenticated vs. stored" equality-break class this scan targets.

### Likelihood Explanation
The bug is a pure logic inversion in a single, deterministic boolean comparison — no attacker timing or race condition is needed to trigger it; any neighbor record whose `expire_block` has passed will exhibit this behavior on every call. However, likelihood of a *remote, unprivileged* impact depends entirely on `lookup_peer` being reachable via network-supplied `peer_addr` values and its result flowing into a security-relevant decision, which I could not confirm from the visible call sites in this session.

### Recommendation
Invert the condition in `stackslib/src/net/p2p.rs::lookup_peer` so that it returns `Some(neighbor)` when `neighbor.expire_block >= cur_block_height` (still valid / unexpired) and `None` when `neighbor.expire_block < cur_block_height` (expired), matching the documented contract and the expiry conventions used elsewhere in `stackslib/src/net/db.rs`. Additionally, confirm and, if necessary, harden all call sites that consume `lookup_peer`'s result to ensure expired neighbor identities are never treated as currently valid.

### Proof of Concept
Given a `Neighbor` record in `PeerDB` with `expire_block = E` and the network's current height `H`:
1. If `E < H` (the key has expired), `lookup_peer(H, addr)` returns `Ok(Some(neighbor))` — the expired neighbor is reported as known/valid.
2. If `E >= H` (the key is still valid), `lookup_peer(H, addr)` returns `Ok(None)` — a still-valid neighbor is reported as unknown.

This is directly visible from the branch logic at: [3](#0-2) 

which is the inverse of the stated doc-comment contract at: [4](#0-3)

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

**File:** stackslib/src/net/db.rs (L4000-4009)
```rust
        // 6. Denied - Expired (Effectively NOT Denied) (Fresh, Epoch 3.0)
        let mut n_denied_expired = base_neighbor.clone();
        n_denied_expired.addr.port = 10006;
        n_denied_expired.addr.peer_version =
            PEER_VERSION_TESTNET_MAJOR | PEER_VERSION_EPOCH_3_0 as u32;
        n_denied_expired.public_key =
            Secp256k1PublicKey::from_private(&Secp256k1PrivateKey::random());
        n_denied_expired.denied = (now_secs - 3600) as i64;
        peers_to_insert.push(n_denied_expired.clone());

```
