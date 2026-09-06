### Title
`PeerAddress::is_in_private_range()` fails to exclude multicast/reserved ranges, letting forged non-routable peer records be accepted, stored, and gossiped as canonical - ([File: stacks-common/src/types/net.rs])

### Summary
`PeerAddress::is_in_private_range()` only checks `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8` (IPv4) and `fc00::/7`/`::1` (IPv6). It does not exclude multicast (`224.0.0.0/4` IPv4, `ff00::/8` IPv6) or other reserved/non-unicast ranges. This is the same bug class as CVE-2025-8020 (`private-ip` npm package SSRF via unfiltered multicast ranges): a filter meant to reject non-routable/attacker-controlled destinations has an incomplete range table. [1](#0-0) 

### Finding Description
Every gate in the P2P layer that is supposed to reject unroutable/bogus peer addresses relies on the pair `is_anynet()` + `is_in_private_range()`:
- `PeerNetwork::can_register_peer` uses `is_in_private_range()` to refuse registering peers with non-routable addresses. [2](#0-1) 
- `NeighborWalk::filter_sensible_neighbors` and `handle_handshake_accept` use the same check to decide which gossiped `NeighborAddress` entries are "sensible" (routable) and should be added to the frontier / PeerDB. [3](#0-2) [4](#0-3) 
- `liststackerdbreplicas` RPC and `stackerdb/sync::find_qualified_replicas` use it to filter which peer records are exposed/queried for StackerDB replication. [5](#0-4) [6](#0-5) 

None of these call sites check for multicast (`224.0.0.0/4`, `ff00::/8`) or other reserved non-unicast ranges. A remote, unauthenticated peer can advertise itself (in a `HandshakeAccept`, `GetNeighborsReply`, or PeerDB record propagated via gossip) with an address such as `224.0.0.1`. Because `is_anynet()` only matches `0.0.0.0`/`::` and `is_in_private_range()` only matches the RFC1918/loopback/ULA ranges, this address passes the "is this routable/sensible" filters and is treated as a legitimate public neighbor: it gets inserted into `new_frontier`/PeerDB via `handle_handshake_accept` and `update_neighbor`, and subsequently re-served to other nodes as a "sensible"/public neighbor through `GetNeighbors` responses and the `liststackerdbreplicas` RPC endpoint. This is a case of non-canonical/attacker-forged address data being accepted and propagated as canonical, unroutable network state - the direct analog of the private-ip package treating multicast IPs as "not private" and thus not blocking SSRF-style requests toward them.

### Impact Explanation
This does not directly cause an SSRF exfiltration (Stacks P2P connections are outbound TCP, and TCP to a multicast destination will simply fail to connect), but it does allow an attacker to inject and have the network propagate forged, non-canonical peer-address records that a correct filter should have rejected. Consequences:
- Poisoning of local and downstream `PeerDB` frontier data with reserved-range addresses, since `filter_sensible_neighbors` and `handle_handshake_accept` incorrectly classify them as "routable"/public and persist them via `update_neighbor`.
- Network-wide propagation of this forged data as other nodes crawl and re-gossip these "sensible" neighbor entries, and as `liststackerdbreplicas` serves them out as legitimate public StackerDB replicas.
- Wasted connection attempts/resources across the network as each node that receives these entries tries to walk/handshake to them.

This lands in the "High" bucket defined by the rules (serving non-canonical state as canonical / steering peers via false inventory), since it does not grant unauthenticated writes to consensus state or cause a crash, but does corrupt the peer-discovery data set that is treated as canonical/routable.

### Likelihood Explanation
Trivial to trigger: any remote, unauthenticated peer that completes (or attempts) a handshake, or is listed via `GetNeighbors`, can set its `NeighborAddress`/`PeerAddress` bytes to any multicast IP. No cryptographic material beyond a normal handshake keypair is required, and no state/consensus interaction is needed — this is purely a network-address-hygiene filter, which is bypassed for the entire `224.0.0.0/4` and `ff00::/8` ranges (and other non-RFC1918 reserved ranges) on every code path that calls `is_in_private_range()`.

### Recommendation
Extend `PeerAddress::is_in_private_range()` (or add a companion `is_routable()`/`is_globally_reachable()` check used at all these call sites) to also reject:
- IPv4 multicast `224.0.0.0/4` and reserved `240.0.0.0/4`, link-local `169.254.0.0/16`, and CGNAT `100.64.0.0/10`.
- IPv6 multicast `ff00::/8` and link-local `fe80::/10`.

Apply this consistently at all sites currently gating on `is_anynet() || is_in_private_range()`: `PeerNetwork::can_register_peer` (`stackslib/src/net/p2p.rs`), `NeighborWalk::filter_sensible_neighbors`/`handle_handshake_accept` (`stackslib/src/net/neighbors/walk.rs`), `liststackerdbreplicas` (`stackslib/src/net/api/liststackerdbreplicas.rs`), and `stackerdb/sync::find_qualified_replicas`.

### Proof of Concept
1. Run a malicious peer node that completes a handshake with a target Stacks node, but reports its `NeighborAddress.addrbytes` as `224.0.0.1` (or advertises this address inside a `GetNeighborsReply`/`HandshakeAcceptData`).
2. On the target, `NeighborWalk::filter_sensible_neighbors` / `handle_handshake_accept` evaluate `is_anynet()` (false) and `is_in_private_range()` (false, since `0x0e0.0.0.1` falls outside all listed ranges) and treat the address as a normal public/"sensible" neighbor. [4](#0-3) 
3. The address is persisted to `PeerDB` via `update_neighbor` and is subsequently served back out to other nodes as a valid neighbor/replica via `GetNeighbors` responses and `liststackerdbreplicas`. [5](#0-4) 
4. Repeat with many distinct multicast/reserved addresses to pollute the frontier of the target and any node that later crawls it, causing wasted connection attempts network-wide.

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

**File:** stackslib/src/net/neighbors/walk.rs (L689-703)
```rust
        // if the neighbor accidentally gave us a private IP address, then
        // just use the one we used to contact it.  This can happen if the
        // node is behind a load-balancer, or is doing port-forwarding,
        // etc. But do nothing if both cur_neighbor and its reported address are private.
        if (neighbor_from_handshake.addr.addrbytes.is_in_private_range()
            || neighbor_from_handshake.addr.addrbytes.is_anynet())
            && !self.cur_neighbor.addr.addrbytes.is_in_private_range()
        {
            debug!(
                "{}: outbound neighbor gave private IP address {:?}; assuming it meant {:?}",
                local_peer_str, &neighbor_from_handshake.addr, &self.cur_neighbor.addr
            );
            neighbor_from_handshake.addr.addrbytes = self.cur_neighbor.addr.addrbytes.clone();
            neighbor_from_handshake.addr.port = self.cur_neighbor.addr.port;
        }
```

**File:** stackslib/src/net/api/liststackerdbreplicas.rs (L126-141)
```rust
        let mut naddrs = match replicas_resp {
            Ok(neighbors) => neighbors
                .into_iter()
                .map(|neighbor| NeighborAddress::from_neighbor(&neighbor))
                .filter(|naddr| {
                    if naddr.addrbytes.is_anynet() {
                        // don't expose 0.0.0.0 or ::1
                        return false;
                    }
                    if !allow_private && naddr.addrbytes.is_in_private_range() {
                        // filter unroutable network addresses
                        return false;
                    }
                    true
                })
                .collect::<Vec<_>>(),
```

**File:** stackslib/src/net/stackerdb/sync.rs (L111-125)
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
            });
```
