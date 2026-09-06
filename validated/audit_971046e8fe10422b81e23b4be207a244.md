## Title
SSRF guard bypass via unfiltered 169.254.0.0/16 (link-local / cloud metadata) addresses - (File: stacks-common/src/types/net.rs)

### Summary
`PeerAddress::is_in_private_range()` is the network's SSRF/anti-routability guard: it decides whether a peer-supplied IP (from handshakes, neighbor gossip, StackerDB hint-replicas, or API listings) is treated as "private" and therefore excluded from outbound connection attempts, neighbor-walk targets, mempool/StackerDB queries, and public API peer listings. Its IPv4 branch only tests for `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, and `127.0.0.0/8`, but omits `169.254.0.0/16` (IPv4 link-local, which includes `169.254.169.254`, the canonical cloud-provider metadata address on AWS/GCP/Azure/OpenStack). An attacker-supplied address in this range is classified as "public" and is allowed through every gate that relies on this function.

### Finding Description
`is_in_private_range()`:
```rust
pub fn is_in_private_range(&self) -> bool {
    if self.is_ipv4() {
        // 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, or 127.0.0.0/8
        self.0[12] == 10
            || (self.0[12] == 172 && self.0[13] >= 16 && self.0[13] <= 31)
            || (self.0[12] == 192 && self.0[13] == 168)
            || self.0[12] == 127
    } else {
        ...
    }
}
``` [1](#0-0) 

This is documented and relied upon as the node's private/unroutable filter across the p2p and StackerDB stacks:
- `PeerNetwork::can_register_peer` rejects/accepts inbound and outbound peer registration based on this check when `private_neighbors` is disabled. [2](#0-1) 
- `NeighborWalk::filter_sensible_neighbors` drops gossiped neighbor addresses that are in the private range before the walk connects to them. [3](#0-2) 
- `eval_hint_replicas` in the StackerDB config loader — which parses an **attacker-deployed Clarity contract's** `hint-replicas` list — explicitly filters out private addresses before adding them to `hint_replicas`, which the node subsequently treats as legitimate remote replicas to contact for StackerDB chunk sync. [4](#0-3) 
- The config doc for `private_neighbors` states this exact guard governs "connections and interactions with peers having private IP addresses" including mempool/StackerDB queries and API peer listings. [5](#0-4) 

Because the IPv4 branch never tests `169.254.0.0/16`, an address such as `169.254.169.254` (embedded as raw bytes in a `PeerAddress`, e.g. via a StackerDB contract's `hint-replicas` list, a gossiped `NeighborAddress`, or a handshake) is classified as *not* private. It therefore passes every one of the above gates and the node will treat it as a normal, routable peer/replica — attempting outbound TCP connections to it (StackerDB chunk fetch, neighbor handshake) exactly as it would for a legitimate public peer.

### Impact Explanation
This is a direct analog of the reported bug class: an SSRF guard that fails to recognize a well-known non-public address range, allowing the node to be steered into making outbound requests to sensitive internal targets — most notably cloud metadata services (`169.254.169.254`), which frequently expose IAM credentials, instance identity documents, and other sensitive data. The most concrete, remote, unprivileged trigger is the StackerDB `hint-replicas` path: any account can deploy a Clarity contract implementing the `stackerdb-trait` with a `hint-replicas` entry whose `addr` encodes `169.254.169.254`; once a node's StackerDB config is refreshed from that contract, the node adds this address to its list of remote replicas and will attempt to fetch StackerDB chunks from it, causing the node to issue outbound HTTP requests to the metadata endpoint.

### Likelihood Explanation
Likelihood is high: no privileged role, secret key, or victim cooperation is required — any party can deploy a contract satisfying the StackerDB trait interface with a crafted `hint-replicas` entry, and nodes that subscribe to that StackerDB config will automatically parse and act on it. The check is a pure byte-range comparison with an obvious blind spot (`169.254.0.0/16` never tested), making the bypass trivial and deterministic.

### Recommendation
Extend `PeerAddress::is_in_private_range()`'s IPv4 branch to also match `169.254.0.0/16` (`self.0[12] == 169 && self.0[13] == 254`), and audit for other commonly-required non-public ranges (e.g., `0.0.0.0/8`, CGNAT `100.64.0.0/10`, multicast/reserved) that a security-sensitive SSRF-style guard should reject, consistent with the guard's documented purpose in `stackslib/src/config/mod.rs`.

### Proof of Concept
1. Construct a `PeerAddress` whose IPv4-mapped bytes decode to `169.254.169.254`:
```rust
let metadata_addr = PeerAddress::from_ipv4(169, 254, 169, 254);
assert!(!metadata_addr.is_in_private_range()); // currently returns false (bug)
```
2. Deploy a Clarity contract implementing `stackerdb-trait` whose `stackerdb-get-config` response's `hint-replicas` list contains an entry with `addr` = the 16-byte IPv4-mapped encoding of `169.254.169.254` (`0,0,0,0,0,0,0,0,0,0,0xff,0xff,169,254,169,254`), a valid port (≥1024), and a `public-key-hash`.
3. When `eval_hint_replicas` (stackerdb/config.rs) processes this contract, the address passes the `is_in_private_range()` filter and is added to `hint_replicas` [6](#0-5) , causing the node to later attempt an outbound connection/StackerDB chunk request to `169.254.169.254:<port>` when syncing that StackerDB.

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

**File:** stackslib/src/net/stackerdb/config.rs (L355-369)
```rust
            let peer_addr = PeerAddress::from_slice(&addr_bytes).expect("FATAL: not 16 bytes");
            if peer_addr.is_in_private_range() {
                debug!(
                    "Ignoring private IP address '{}' in hint-replicas",
                    &peer_addr.to_socketaddr(port as u16)
                );
                continue;
            }

            let naddr = NeighborAddress {
                addrbytes: peer_addr,
                port: port as u16,
                public_key_hash: Hash160(*pubkey_hash_slice),
            };
            hint_replicas.push(naddr);
```

**File:** stackslib/src/config/mod.rs (L3788-3796)
```rust
    /// Whether to allow connections and interactions with peers having private IP addresses.
    ///
    /// If `false` (default), the node will generally:
    /// - Reject incoming connection attempts from peers with private IPs.
    /// - Avoid initiating connections to peers known to have private IPs.
    /// - Ignore peers with private IPs during neighbor discovery (walks).
    /// - Skip querying peers with private IPs for mempool or StackerDB data.
    /// - Filter out peers with private IPs from API responses listing potential peers.
    ///
```
