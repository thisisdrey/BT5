### Title
NAT64-mapped IPv4 addresses bypass the P2P/StackerDB private-address filter, allowing remote peers to force outbound connections to internal hosts - (File: `stacks-common/src/types/net.rs`)

### Summary
`PeerAddress::is_in_private_range()` is the sole gate used throughout `stackslib/src/net/**` to decide whether a peer-supplied address is "private" and must be refused for outbound connections, gossip relay, and StackerDB replica selection. Like the CC-Tweaked `PrivatePattern.matches()` bug, this function enumerates known private ranges but never recognizes the RFC 6052 NAT64 well-known prefix `64:ff9b::/96`. A remote, unauthenticated peer can therefore gossip a `NeighborAddress` whose `addrbytes` encode an internal IPv4 host as `64:ff9b::<hex ipv4>`, and the victim node's private-IP filter will treat it as "public," causing the node to actually open a TCP connection (and send a Handshake) to that internal host whenever NAT64 routing is present on the network.

### Finding Description
The private-range check is: [1](#0-0) 

```rust
pub fn is_in_private_range(&self) -> bool {
    if self.is_ipv4() {
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

For a NAT64 address such as `64:ff9b::0a00:0111` (encoding internal `10.0.1.17`), `is_ipv4()` is `false` (it is not the `::ffff:` mapped form), so the function falls into the else-branch, and `self.0[0]` is `0x00`, which is neither `>= 0xfc` nor `::1`. The function returns `false` — the address is misclassified as public.

This is the load-bearing check used to enforce `private_neighbors = false` (the default) across the P2P/StackerDB layer:
- Filtering gossiped `NeighborAddress` lists: [2](#0-1) 
- Filtering inbound peer registration: [3](#0-2) 
- Filtering StackerDB hint-replicas selected for sync: [4](#0-3) 
- Filtering mempool sync targets: [5](#0-4) 

Once a NAT64-crafted `NeighborAddress` survives `filter_sensible_neighbors`, it is passed straight into the handshake/connect pipeline: [6](#0-5) 

which calls `comms.neighbor_session_begin` → `neighbor_connect_and_handshake` → `network.connect_peer`: [7](#0-6) [8](#0-7) 

```rust
let sock = NetworkState::connect(
    &neighbor.addrbytes.to_socketaddr(neighbor.port),
    ...
)?;
```

`to_socketaddr` converts the raw 16 bytes directly to an `Ipv6Addr`/`SocketAddr` with no further validation: [9](#0-8) . The resulting real TCP connect (`mio_net::TcpStream::connect`) is issued unconditionally: [10](#0-9) .

### Impact Explanation
On any deployment where the node's host has NAT64/DNS64 routing configured (a standard configuration on AWS/GCP IPv6-only subnets), an unauthenticated remote peer can supply gossiped `NeighborAddress` records or StackerDB hint-replica addresses that are silently exempted from the node's own `private_neighbors = false` policy. The node will then perform outbound TCP connect + P2P Handshake attempts to arbitrary internal IPv4 endpoints of the operator's choosing, chosen entirely by the attacker. This is an SSRF-class bypass of an explicit security control ("Reject peers with private IPs... Skip querying peers with private IPs for mempool or StackerDB data" — documented at [11](#0-10) ) — the auth-gate meant to prevent exactly this fails open for NAT64-encoded addresses.

### Likelihood Explanation
No authentication or privileged access is required; any peer that can respond to a `GetNeighbors` request, or be listed as a StackerDB hint-replica/mempool target, can supply the malicious address. The precondition (NAT64 routing) is increasingly common on cloud-hosted nodes following IPv6-only subnet adoption, matching the same precondition cited in the source advisory.

### Recommendation
Extend `PeerAddress::is_in_private_range()` (and any equivalent classification path) to explicitly detect and reject the `64:ff9b::/96` NAT64 well-known prefix (and ideally other IPv6 transition mechanisms such as `2002::/16` 6to4, which is separately handled elsewhere but not here), mirroring the check bytes `[0]==0x00, [1]==0x64, [2]==0xff, [3]==0x9b, [4..12]==0`.

### Proof of Concept
1. Stand up a malicious Stacks peer that a target node will crawl (e.g., listed as an initial/seed neighbor, or discovered via the walk).
2. When the target sends `GetNeighbors`, reply with a `NeighborsData` containing one `NeighborAddress` whose `addrbytes` is:
   `PeerAddress([0x00,0x64,0xff,0x9b,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x0a,0x00,0x01,0x11])`
   (i.e. `64:ff9b::0a00:0111`, encoding internal target `10.0.1.17`), with any port and a public-key hash of the attacker's choosing.
3. Because `is_in_private_range()` returns `false` for this address, `filter_sensible_neighbors` at [2](#0-1)  does not filter it out even with `private_neighbors=false`.
4. The victim's `neighbor_handshakes_begin` proceeds to call `comms.neighbor_session_begin` on this address, triggering `PeerNetwork::connect_peer` → `NetworkState::connect(&addr.to_socketaddr(port), ...)`, opening a real TCP connection to `64:ff9b::0a00:0111:<port>`.
5. On a network with NAT64 routing (`64:ff9b::/96 → NAT gateway`), this connection is translated to `10.0.1.17:<port>`, reaching the internal host that the `private_neighbors` policy was intended to shield.

### Citations

**File:** stacks-common/src/types/net.rs (L144-148)
```rust
    /// Convert to SocketAddr
    pub fn to_socketaddr(&self, port: u16) -> SocketAddr {
        let ip_addr = self.to_ipaddr();
        SocketAddr::new(ip_addr, port)
    }
```

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

**File:** stackslib/src/net/neighbors/walk.rs (L1004-1041)
```rust
        for na in pending_neighbor_addrs.into_iter() {
            // don't talk to myself if we're listed as a neighbor of this
            // remote peer.
            if na.public_key_hash == my_pubkey_hash {
                test_debug!(
                    "{:?}: skip handshaking with myself",
                    network.get_local_peer()
                );
                continue;
            }

            // don't handshake with cur_neighbor if we already know its public IP
            // address (we may not know this if the neighbor is inbound)
            if na.addrbytes == self.cur_neighbor.addr.addrbytes
                && na.port == self.cur_neighbor.addr.port
            {
                test_debug!(
                    "{:?}: skip handshaking with cur_neighbor {:?}",
                    network.get_local_peer(),
                    &self.cur_neighbor.addr
                );
                continue;
            }

            let nk = na.to_neighbor_key(network);

            // don't talk to a neighbor if it's unroutable anyway
            if network.is_bound(&nk) || nk.addrbytes.is_anynet() {
                test_debug!(
                    "{:?}: will not connect to bind / anynet address {:?}",
                    network.get_local_peer(),
                    &nk
                );
                continue;
            }

            // start a session with this neighbor
            match self.comms.neighbor_session_begin(network, &na) {
```

**File:** stackslib/src/net/p2p.rs (L1435-1445)
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

**File:** stackslib/src/net/neighbors/comms.rs (L150-227)
```rust
    fn neighbor_connect_and_handshake<NK: ToNeighborKey>(
        &mut self,
        network: &mut PeerNetwork,
        neighbor_addr: &NK,
    ) -> Result<Option<ReplyHandleP2P>, net_error> {
        let nk = neighbor_addr.to_neighbor_key(network);
        if network.is_registered(&nk) {
            // already connected
            self.remove_connecting(network, &nk);
            return self.neighbor_handshake(network, &nk).map(Some);
        }

        if let Some(event_id) = self.get_connecting(network, &nk) {
            // is the peer network still working?
            if !network.is_connecting(event_id) {
                debug!("{:?}: Failed to connect to {:?} (event {} no longer connecting; assumed timed out)", network.get_local_peer(), event_id, &nk);
                self.remove_connecting_error(network, &nk);
                return Err(net_error::PeerNotConnected(format!(
                    "Failed to connect to {nk} (event {event_id} no longer connecting; assumed timed out)",
                )));
            }

            // still connecting
            debug!(
                "{:?}: still connecting to {:?} (event {})",
                network.get_local_peer(),
                &nk,
                event_id
            );
            return Ok(None);
        }

        match network.can_register_peer(&nk, true) {
            Ok(_) => {
                let event_id = network.connect_peer(&nk).map_err(|e| {
                    debug!(
                        "{:?}: Failed to connect to {:?}: {:?}",
                        network.get_local_peer(),
                        &nk,
                        &e
                    );
                    net_error::PeerNotConnected(format!("Failed to connect to {nk}: {e}"))
                })?;

                // remember this in the walk result
                self.add_connecting(network, &nk, event_id);

                // force the caller to try again -- we're not registered yet
                debug!(
                    "{:?}: Connecting to {:?} (event {})",
                    network.get_local_peer(),
                    &nk,
                    event_id
                );
                return Ok(None);
            }
            Err(net_error::AlreadyConnected(_event_id, alt_nk)) => {
                test_debug!(
                    "{:?}: already connected to {:?} as event {} ({:?})",
                    network.get_local_peer(),
                    &nk,
                    _event_id,
                    &alt_nk
                );
                self.remove_connecting(network, &alt_nk);
                return self.neighbor_handshake(network, &alt_nk).map(Some);
            }
            Err(e) => {
                info!(
                    "{:?}: could not connect to {:?}: {:?}",
                    network.get_local_peer(),
                    &nk,
                    &e
                );
                return Err(e);
            }
        }
    }
```

**File:** stackslib/src/net/poll.rs (L298-309)
```rust
    /// Connect to a remote peer, but don't register it with the poll handle.
    /// The underlying connect(2) is _asynchronous_, so the caller will need to register it with a
    /// poll handle and wait for it to be connected.
    pub fn connect(
        addr: &SocketAddr,
        socket_send_buffer: u32,
        socket_recv_buffer: u32,
    ) -> Result<mio_net::TcpStream, net_error> {
        let stream = mio_net::TcpStream::connect(addr).map_err(|_e| {
            test_debug!("Failed to convert to mio stream: {:?}", &_e);
            net_error::ConnectionError
        })?;
```

**File:** stackslib/src/config/mod.rs (L3788-3801)
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
    /// Setting this to `true` disables these restrictions, which can be useful for
    /// local testing environments or fully private network deployments.
    /// ---
    /// @default: `false`
    pub private_neighbors: Option<bool>,
```
