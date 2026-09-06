### Title
IPv6 6to4/NAT64 tunneled-address bypass of the "private IP" gossip/hint-replica filter enables node-driven SSRF/pivot into internal networks - (File: stacks-common/src/types/net.rs)

### Summary
`PeerAddress::is_in_private_range()` is the single security gate used throughout `stackslib/src/net/**` to decide whether an attacker/peer-supplied IPv6 address should be treated as internal/private and therefore refused for gossip relay, connection, or StackerDB replication. Its IPv6 branch only special-cases `fc00::/7` (ULA) and `::1`, missing IPv4-embedding tunneling prefixes such as `2002::/16` (6to4) and `64:ff9b::/96` (NAT64/RFC 6052), exactly the bug class described in the external report for the MCP Registry SSRF (GHSA-r48c-v28r-pf6v).

### Finding Description
`is_in_private_range()` is defined as: [1](#0-0) 

For IPv6 it only returns `true` when the first octet is `>= 0xfc` (covering ULA `fc00::/7`, link-local `fe80::/10`, and deprecated site-local `fec0::/10`, since all of these have a first byte of `0xfc`–`0xff`) or for the exact loopback `::1`. It does **not** check:
- `2002::/16` (6to4) — bits 16–47 embed an arbitrary IPv4 address (first byte `0x20`)
- `64:ff9b::/96` (NAT64, RFC 6052) — the low 32 bits embed an arbitrary IPv4 address (first byte `0x00`)
- `64:ff9b:1::/48` (RFC 8215 local-use NAT64)

Both prefix families let a remote party craft an IPv6 literal that decodes/routes to an internal IPv4 address (RFC1918, loopback, or link-local/metadata range) while sailing straight through this filter as "not private."

This single helper is relied upon as the *only* internal/private-address gate in several remote-facing, unauthenticated code paths in scope:

1. **StackerDB hint-replica addresses from a smart contract** — `eval_hint_replicas` decodes an operator/attacker-deployed Clarity contract's `hint-replicas` list into raw 16-byte `PeerAddress` values and only skips them if `peer_addr.is_in_private_range()`: [2](#0-1) 
Any contract deployer (unauthenticated w.r.t. the node — deploying a contract requires only an on-chain transaction, not any node privilege) can specify a hint-replica address such as `64:ff9b::a9fe:a9fe` (NAT64-encoded `169.254.169.254`) or `2002:0a00:0001::` (6to4-encoded `10.0.0.1`), which the check will not filter out.

2. **Gossip-relayed neighbor addresses** — `filter_sensible_neighbors`, used during the peer-walk state machine to decide which gossiped `NeighborAddress` entries are "routable" and worth walking to/relaying, uses the same predicate: [3](#0-2) 
and it is also used to auto-substitute an unroutable self-reported address during handshake accept: [4](#0-3) 
A malicious peer can report itself, or gossip a third-party `NeighborAddress`, using a 6to4/NAT64-encoded internal-IPv4 IPv6 literal; the address will be accepted as "sensible"/public and other nodes across the network may attempt outbound TCP connections to it via `PeerNetwork::connect_peer` → `NetworkState::connect`: [5](#0-4) 

3. **Mempool sync target selection** also relies on the same check before issuing an outbound query: [6](#0-5) 

### Impact Explanation
Because this filter is the network's only defense against directing peers toward "private"/internal address space, and it is bypassed by trivially-encodable IPv6 tunneling prefixes, an unauthenticated remote party (a peer on the P2P network, or any account able to deploy a StackerDB-config smart contract) can cause honest Stacks nodes to originate outbound TCP connections toward attacker-chosen internal IPv4 targets (RFC1918 ranges, loopback, or cloud metadata-style link-local addresses) that would otherwise have been blocked. This is a network-wide SSRF/pivot primitive: it can be propagated via gossip (`NeighborAddress`) so that many nodes, not just the reporting node, are induced to dial the forged target, and it can be embedded durably in an on-chain StackerDB config contract so every node that syncs that DB attempts the connection. This satisfies the in-scope "forged gossip relayed" / cross-node SSRF criterion and goes beyond affecting only the attacker's own node.

The severity is bounded by the fact that connections are plain TCP dials into the Stacks P2P/HTTP handshake protocol (not an arbitrary raw HTTP fetch as in the origin MCP report) — success requires the internal target to also speak a protocol the p2p/stackerdb/mempool client can interpret, and it depends on host-level NAT64/6to4 routing being present, consistent with the AC:High characterization in the original advisory.

### Likelihood Explanation
Exploitation requires only:
- For the gossip path: being any P2P peer able to send a `HandshakeAccept` or neighbor-list gossip message (no authentication beyond a completed handshake) with a crafted `NeighborAddress`.
- For the StackerDB path: deploying a Clarity contract with attacker-chosen `hint-replicas` (or being a Stacker/miner controlling the config contract) referencing a crafted IPv6 literal.

Both require no node secrets or privileged roles, matching the "unprivileged" threshold. Actual network reachability further depends on the dialing node's host having 6to4/NAT64 routing enabled — a non-default but plausible condition on IPv6-capable/dual-stack deployments, mirroring the original advisory's AC:High rating.

### Recommendation
Extend `PeerAddress::is_in_private_range()` (or add a dedicated helper called from it) to reject IPv6 addresses in `2002::/16`, `64:ff9b::/96`, and `64:ff9b:1::/48`, in addition to the existing `fc00::/7`/loopback checks, mirroring the fix pattern used for GHSA-56c3-vfp2-5qqj / GHSA-r48c-v28r-pf6v. Add regression tests asserting that `is_in_private_range()` returns `true` for representative addresses in each of these prefixes when they encode RFC1918/loopback/link-local IPv4 targets (e.g., `2002:0a00:0001::`, `64:ff9b::a9fe:a9fe`), and ensure `eval_hint_replicas`, `filter_sensible_neighbors`, and the mempool-sync gate all consume the corrected predicate.

### Proof of Concept
1. Deploy (or have any account deploy) a StackerDB-config Clarity contract whose `hint-replicas` list contains an entry with `addr` bytes equal to the 16-byte encoding of `64:ff9b::a9fe:a9fe` (NAT64 encoding of `169.254.169.254`) and a valid `port`/`public-key-hash`.
2. Have a node load this StackerDB config; `eval_hint_replicas` in `stackslib/src/net/stackerdb/config.rs:355-362` calls `peer_addr.is_in_private_range()`, which returns `false` for this address (first octet `0x00`, not `>= 0xfc`, and not `::1`), so the hint replica is accepted rather than skipped.
3. As the StackerDB sync logic later attempts to contact configured/hinted replicas, the node originates an outbound connection toward the NAT64-embedded `169.254.169.254` (or any RFC1918 address similarly encoded), a target that the private-range filter was intended to block.
4. Equivalently, a malicious P2P peer can report a `HandshakeAcceptData` with `addr` set to the same NAT64-encoded literal; `filter_sensible_neighbors` (`stackslib/src/net/neighbors/walk.rs:584-594`) will not filter it out, and it can be relayed to/acted on by other nodes during neighbor walks.

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

**File:** stackslib/src/net/stackerdb/config.rs (L355-362)
```rust
            let peer_addr = PeerAddress::from_slice(&addr_bytes).expect("FATAL: not 16 bytes");
            if peer_addr.is_in_private_range() {
                debug!(
                    "Ignoring private IP address '{}' in hint-replicas",
                    &peer_addr.to_socketaddr(port as u16)
                );
                continue;
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

**File:** stackslib/src/net/p2p.rs (L1435-1448)
```rust
        let next_event_id = match self.network {
            None => {
                debug!("{:?}: network not connected", &self.local_peer);
                return Err(net_error::NotConnected);
            }
            Some(ref mut network) => {
                let sock = NetworkState::connect(
                    &neighbor.addrbytes.to_socketaddr(neighbor.port),
                    self.connection_opts.socket_send_buffer_size,
                    self.connection_opts.socket_recv_buffer_size,
                )?;
                let hint_event_id = network.next_event_id()?;
                let registered_event_id =
                    network.register(self.p2p_network_handle, hint_event_id, &sock)?;
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
