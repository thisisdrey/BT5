## Analysis

The Gotenberg bug class (`IsPublicIP` failing to recognize IPv4 link-local / IPv6 6to4 / NAT64 / deprecated site-local prefixes as non-public, letting a remote unauthenticated party steer outbound requests to internal targets like cloud metadata `169.254.169.254`) has a direct structural analog in this repo's private-IP deny-list function.

### Title
Remote deny-list bypass in `PeerAddress::is_in_private_range` via IPv4 link-local (169.254.0.0/16) and IPv6 6to4/NAT64 prefixes enables SSRF to internal-only destinations - (File: `stacks-common/src/types/net.rs`)

### Summary
`PeerAddress::is_in_private_range()`, the sole gatekeeper the P2P/mempool/StackerDB subsystems use to decide whether a peer-supplied address/URL is "private" (and therefore should be skipped when `private_neighbors=false`, the default), only checks `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8` for IPv4 and `fc00::/7`/`::1` for IPv6: [1](#0-0) 

It never checks IPv4 link-local `169.254.0.0/16` (the exact prefix hosting AWS/GCP/Azure IMDS), nor IPv6 6to4 (`2002::/16`) or NAT64 (`64:ff9b::/96`) tunneling prefixes that embed an IPv4 address in their low bits. Any address in these ranges is classified "public" and is allowed through every filter built on top of this function.

### Finding Description
This function gates multiple remote-reachable decision points that a connected-but-otherwise-unprivileged peer can influence:

- `PeerNetwork::can_register_peer` uses it to reject inbound/outbound peer registration when the neighbor's address is private: [2](#0-1) 

- `NeighborWalk::filter_sensible_neighbors` uses it to decide which gossiped `NeighborAddress` entries are "routable" during peer-graph walks (data supplied by remote peers via `GetNeighbors`/handshake gossip): [3](#0-2) 

- `StackerDBSync::find_qualified_replicas` uses it to decide which StackerDB replica addresses (sourced from the PeerDB, populated from peer-advertised handshake/hint data) are queried for chunk data: [4](#0-3) 

- Mempool sync uses it to decide whether to skip an outbound query to a peer's self-reported `data_url` before issuing an actual HTTP request: [5](#0-4) 

Because `169.254.0.0/16`, `2002::/16`, and `64:ff9b::/96` all fall through the private-range check as "public" (`self.0[12]==10 || 172.16-31 || 192.168 || 127` for IPv4, and `self.0[0]>=0xfc` for IPv6, neither of which matches `169.x` or `0x20/0x00`-leading IPv6 words), a remote peer that advertises an address or URL in those ranges (via handshake `data_url`, gossiped `NeighborAddress`, or StackerDB hint replicas) is treated by the victim node as a normal public destination, and the node's own networking code will dial/HTTP-fetch it — bypassing the operator's `private_neighbors=false` (default) intent to keep the node from touching internal-only network space.

### Impact Explanation
On any cloud-hosted node (the deployment model explicitly anticipated by the original report), this bypass lets a network peer redirect the victim's own outbound P2P/mempool/StackerDB machinery at `169.254.169.254` (IMDS) or other internal-only, link-local-addressed infrastructure that the private-range filter was specifically meant to keep unreachable, defeating the intended network-segmentation control (CWE-918/CWE-184 analog to the reported issue).

### Likelihood Explanation
Reaching this code only requires becoming a connected peer (handshake) or being selected as a StackerDB hint-replica/gossip source — no signing key, admin role, or node secret is needed, matching the "remote, unauthenticated" bar for this analog set.

### Recommendation
Extend `PeerAddress::is_in_private_range()` (and/or add a dedicated check called at each of the above call sites) to explicitly reject:
- IPv4 `169.254.0.0/16` (link-local, includes cloud IMDS)
- IPv6 `2002::/16` (6to4)
- IPv6 `64:ff9b::/96` and `64:ff9b:1::/48` (NAT64)
- IPv6 `fe80::/10` (link-local) if not already fully covered

### Proof of Concept
`PeerAddress::from_ipv4(169, 254, 169, 254).is_in_private_range()` returns `false` (checked against `stacks-common/src/types/net.rs` lines 202-213: none of the `self.0[12]==10/172/192/127` branches match `169`), so this address is classified public and passes every `!private_neighbors && addr.is_in_private_range()` gate in `p2p.rs`, `walk.rs`, `stackerdb/sync.rs`, and `mempool/mod.rs`, letting a remote peer steer the node's outbound requests at it.

### Citations

**File:** stacks-common/src/types/net.rs (L201-213)
```rust
    /// Is this a private IP address?
    pub fn is_in_private_range(&self) -> bool {
        if self.is_ipv4() {
            // 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, or 127.0.0.0/8
            self.0[12] == 10
                || (self.0[12] == 172 && self.0[13] >= 16 && self.0[13] <= 31)
                || (self.0[12] == 192 && self.0[13] == 168)
                || self.0[12] == 127
        } else {
            // private address (fc00::/7) or localhost (::1)
            self.0[0] >= 0xfc || (self.0[0..15] == [0u8; 15] && self.0[15] == 1)
        }
    }
```

**File:** stackslib/src/net/p2p.rs (L1917-1924)
```rust
        // unroutable?
        if !self.connection_opts.private_neighbors && neighbor_key.addrbytes.is_in_private_range() {
            debug!("{:?}: Peer {:?} is in private range and we are configured to drop private neighbors",
                  &self.local_peer,
                  neighbor_key
            );
            return Err(net_error::Denied);
        }
```

**File:** stackslib/src/net/neighbors/walk.rs (L584-594)
```rust
    /// Select neighbors that are routable, and ignore ones that are not.
    fn filter_sensible_neighbors(
        mut neighbors: Vec<NeighborAddress>,
        private_neighbors: bool,
    ) -> Vec<NeighborAddress> {
        neighbors.retain(|neighbor| !neighbor.addrbytes.is_anynet());
        if !private_neighbors {
            neighbors.retain(|neighbor| !neighbor.addrbytes.is_in_private_range());
        }
        neighbors
    }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L111-124)
```rust
            .filter(|(naddr, _)| {
                if naddr.addrbytes.is_anynet() {
                    return false;
                }
                if naddr.public_key_hash == local_naddr.public_key_hash {
                    // don't talk to us by another address
                    return false;
                }
                if !network.get_connection_opts().private_neighbors
                    && naddr.addrbytes.is_in_private_range()
                {
                    return false;
                }
                true
```

**File:** stackslib/src/net/mempool/mod.rs (L485-498)
```rust
                MempoolSyncState::SendQuery(ref url, ref addr, ref page_id) => {
                    // 3. ask for the remote peer's mempool's novel txs
                    // address must be resolvable
                    if !network.get_connection_opts().private_neighbors
                        && PeerAddress::from_socketaddr(addr).is_in_private_range()
                    {
                        debug!(
                            "{:?}: Mempool sync skips {}, which has private IP",
                            network.get_local_peer(),
                            &addr
                        );
                        self.mempool_sync_reset();
                        return (true, None);
                    }
```
