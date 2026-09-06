[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/net/chat.rs (L2030-2039)
```rust
    fn check_relayer_cycles(relayers: &[RelayData]) -> bool {
        let mut addrs = HashSet::new();
        for r in relayers.iter() {
            if addrs.contains(&r.peer.public_key_hash) {
                return false;
            }
            addrs.insert(r.peer.public_key_hash.clone());
        }
        true
    }
```

**File:** stackslib/src/net/chat.rs (L2042-2050)
```rust
    fn check_relayers_remote(local_peer: &LocalPeer, relayers: &[RelayData]) -> bool {
        let addr = local_peer.to_neighbor_addr();
        for r in relayers.iter() {
            if r.peer.public_key_hash == addr.public_key_hash {
                return false;
            }
        }
        return true;
    }
```

**File:** stackslib/src/net/chat.rs (L2079-2082)
```rust
        for relayer in relayers.iter() {
            self.stats
                .add_relayer(&relayer.peer, (preamble.payload_len - 1) as u64);
        }
```
