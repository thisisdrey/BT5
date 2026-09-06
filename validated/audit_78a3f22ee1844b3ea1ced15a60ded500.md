This looks like a significant logic inversion. Let me examine `lookup_peer` and its usage further.### Title
Inverted expiry check in `PeerNetwork::lookup_peer` returns expired-key neighbors as valid and unexpired ones as unknown - ([File: stackslib/src/net/p2p.rs])

### Summary
`PeerNetwork::lookup_peer` is documented to "Get the neighbor if we know of it and its public key is unexpired," but its comparison is inverted: it returns `Some(neighbor)` precisely when the neighbor's session key **has expired**, and `None` when the key is still valid. This breaks the intended equality between "known, currently-valid peer identity" and what the function actually reports back to its caller.

### Finding Description
The function is: [1](#0-0) 

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

`Neighbor.expire_block` is the burn-block height at which a peer's advertised session public key is revoked/rotated (see `HandshakeData.expire_block_height` and `Neighbor::handshake_update` at [2](#0-1) , and the stale-key check in `validate_handshake` at [3](#0-2)  which correctly treats `expire_block_height <= chain_view.burn_block_height` as "stale"). The correct semantics for "unexpired" is `neighbor.expire_block >= cur_block_height`.

`lookup_peer`'s condition `neighbor.expire_block < cur_block_height` is true exactly when the stored key has **already expired**, yet in that case it returns `Some(neighbor)` (as if found/valid); when the key is still valid (`expire_block >= cur_block_height`), it returns `None` (as if unknown). This is a direct logic inversion of the documented and expected behavior — the equality "stored session key still valid == neighbor returned" is broken in exactly the opposite direction.

### Impact Explanation
This inversion means any caller that relies on `lookup_peer` to distinguish "we have a fresh, still-valid record for this remote address" from "we don't" will get the opposite answer: a peer whose previously-advertised session key has already expired will be reported as a known/looked-up neighbor, while a peer with a currently valid, unexpired key will be reported as not found. Depending on how the caller uses this result (e.g., for admission decisions, deduplication, or re-key/allow-list bookkeeping keyed off of peer identity freshness), this can cause stale/expired peer identity data to be treated as authoritative while current, valid identity data is silently discarded — i.e., non-canonical (expired) state served as canonical current state within the P2P peer-management pipeline.

I was not able to fully trace the downstream call site to `lookup_peer` within the available search iterations (grep matched only the definition plus one other reference in `p2p.rs`, and its content could not be retrieved before the iteration budget ran out), so I cannot state definitively whether the concrete blast radius reaches an unauthenticated write or auth bypass at the network boundary. This uncertainty should be resolved by reading the second `lookup_peer(` call site in `stackslib/src/net/p2p.rs` to confirm what decision is gated by its `Option<Neighbor>` result.

### Likelihood Explanation
This is a pure logic bug reachable on every invocation of `lookup_peer`, not something requiring adversarial input crafting — it triggers deterministically based on ordinary peer-record aging. It is highly likely to be exercised in practice, since `expire_block` values are set on every handshake and naturally cross the `cur_block_height` boundary as the chain progresses (default `private_key_lifetime` is ~1 month, per `stackslib/src/net/connection.rs` defaults). However, the actual security/consensus impact is uncertain pending confirmation of the calling logic.

### Recommendation
Fix the comparison to match the documented intent, e.g.:
```rust
if neighbor.expire_block >= cur_block_height {
    Ok(Some(neighbor))
} else {
    Ok(None)
}
```
and add a unit test asserting `lookup_peer` returns `Some` only for peers with `expire_block >= cur_block_height`, and audit the caller(s) to confirm what security-relevant decision depends on this result.

### Proof of Concept
No exploit script is provided because the affected function is a pure local comparison (`stackslib/src/net/p2p.rs:1798-1824`); the bug is demonstrable purely by code inspection: insert a `Neighbor` with `expire_block = 100` into `PeerDB`, call `lookup_peer(200, addr)` (key already expired at height 200) — this returns `Some(neighbor)`. Conversely, call `lookup_peer(50, addr)` (key still valid) — this returns `None`, contradicting the function's own doc comment "Get the neighbor if we know of it and it's public key is unexpired."

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

**File:** stackslib/src/net/chat.rs (L454-457)
```rust

        self.public_key = pubk;
        self.expire_block = handshake_data.expire_block_height;
        self.last_contact_time = get_epoch_time_secs();
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
