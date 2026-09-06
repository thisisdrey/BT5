I found the key mechanism: `try_resolve_data_url_host` in `stackslib/src/net/chat.rs:2800-2914` resolves an attacker-supplied `data_url` (set verbatim from the peer's `HandshakeData` at `stackslib/src/net/chat.rs:1147`) to `self.data_ip` via DNS or a literal IP, with **no `is_in_private_range()` / `is_anynet()` check anywhere in this path**. That resolved `data_ip` is then used directly by `NeighborRPC::send_request` (`stackslib/src/net/neighbors/rpc.rs:207-233`, used for StackerDB chunk/inventory fetches) to open an outbound HTTP TCP connection via `http.connect_http(...)`, with zero private-range filtering at that call site either.

This contrasts with the mempool-sync path (`stackslib/src/net/mempool/mod.rs:485-498`), which explicitly checks `PeerAddress::from_socketaddr(addr).is_in_private_range()` before connecting, and the neighbor-walk/gossip paths (`stackslib/src/net/neighbors/walk.rs:584-594`, `stackslib/src/net/stackerdb/sync.rs:111-123`), which filter private/loopback addresses before contacting them. The StackerDB HTTP-RPC path (`NeighborRPC::send_request`) and the raw `connect_or_send_http_request` (`stackslib/src/net/httpcore.rs:1877-1911`) skip this check entirely.

### Title
Blind SSRF via unfiltered peer-supplied `data_url` used for StackerDB HTTP fetches - (File: `stackslib/src/net/chat.rs`, `stackslib/src/net/neighbors/rpc.rs`)

### Summary
Any remote peer that completes a P2P handshake can set an arbitrary `data_url` (any hostname/IP:port) in its `HandshakeData`. The node stores it verbatim (`ConversationP2P::update_from_handshake_data`, `stackslib/src/net/chat.rs:1147`) and later resolves it to a socket address via `try_resolve_data_url_host` (`stackslib/src/net/chat.rs:2800-2914`) without checking `is_in_private_range()`/`is_anynet()`. When this peer is later selected as a StackerDB replica, `NeighborRPC::send_request` (`stackslib/src/net/neighbors/rpc.rs:191-249`) uses this resolved address to open an outbound HTTP connection with no equivalent filter, unlike the mempool-sync code path which explicitly guards against private IPs.

### Finding Description
`ConversationP2P::update_from_handshake_data` copies the peer-supplied `handshake_data.data_url` into `self.data_url` unconditionally: [1](#0-0) 
`try_resolve_data_url_host` then resolves this URL (literal IP or DNS name) into `self.data_ip` with no routability/private-range validation: [2](#0-1) 
`NeighborRPC::send_request`, used by the StackerDB sync/RPC machinery to fetch chunks/inventories from remote replicas, takes this `convo.data_ip` and directly calls `http.connect_http` — again with no private-range or anynet filter: [3](#0-2) 
This breaks the intended invariant (enforced elsewhere, e.g. `mempool_sync` and neighbor-walk) that the node should never dial peer/private-range addresses it wasn't explicitly configured to trust. The `PeerNetwork::connect_or_send_http_request` primitive used underneath likewise performs no such check.

### Impact Explanation
An attacker who can complete a handshake (any inbound or outbound unprivileged connection) can steer the node into issuing arbitrary outbound TCP/HTTP requests to internal/private addresses (loopback, RFC1918, link-local, other internal services) reachable from the victim node's network position, with response-derived state machine transitions and errors observable indirectly (StackerDB sync retries/failures, timing). This matches the reported bug class (blind SSRF via internal port scanning) inside the Stacks P2P/StackerDB HTTP-RPC subsystem.

### Likelihood Explanation
Any unprivileged remote node can trigger this simply by completing a P2P handshake and being selected as a StackerDB replica hint/candidate (or via `find_qualified_replicas`, which does filter but only from `PeerDB`-persisted neighbor records — the raw handshake `data_url` string itself is stored and used for the initial DNS resolution/connect attempt regardless). No special privileges, signing keys, or admin roles are required.

### Recommendation
Apply the same `is_in_private_range()` / `is_anynet()` filtering used in `mempool_sync_send_query` (`stackslib/src/net/mempool/mod.rs:488-498`) uniformly to every outbound HTTP dial derived from a peer-supplied `data_url`: specifically inside `ConversationP2P::try_resolve_data_url_host` right after resolving `data_ip` (so a private/loopback resolution is rejected/reset rather than stored), and defensively inside `NeighborRPC::send_request` and `PeerNetwork::connect_or_send_http_request` before calling `connect_http`, unless `connection_opts.private_neighbors` is explicitly enabled.

### Proof of Concept
1. Run two nodes A (victim) and B (attacker-controlled), with `private_neighbors = false` on A.
2. From B, complete the P2P handshake with A, sending `HandshakeData { data_url: "http://169.254.169.254:80/", ... }` (or any internal target reachable from A, e.g. `http://127.0.0.1:<internal-service-port>/`).
3. A stores this `data_url` on the conversation via `update_from_handshake_data` (`stackslib/src/net/chat.rs:1147`).
4. Trigger StackerDB sync where B is selected/hinted as a replica (e.g., via `stackerdb_hint_replicas`/contract hint-replicas pointing to B's `NeighborAddress`, or normal peer discovery once B is in `connected_replicas`).
5. Observe (via a listener on the internal target, or via differing NACK/timeout/error timing in A's StackerDB sync state) that A performs an outbound TCP/HTTP connection attempt to the attacker-chosen internal address — confirming the SSRF primitive, analogous to the reported Elastic finding's port-open/closed oracle.

### Citations

**File:** stackslib/src/net/chat.rs (L1145-1147)
```rust
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();
```

**File:** stackslib/src/net/chat.rs (L2780-2820)
```rust
    /// Try to get the IPv4 or IPv6 address out of a data URL.
    fn try_decode_data_url_ipaddr(data_url: &UrlString) -> Option<SocketAddr> {
        // need to begin resolution
        // NOTE: should always succeed, since a UrlString shouldn't decode unless it's a valid URL or the empty string
        let url = data_url.parse_to_block_url().ok()?;
        let port = url.port_or_known_default()?;
        let ip_addr_opt = match url.host() {
            Some(url::Host::Ipv4(addr)) => {
                // have IPv4 address already
                Some(SocketAddr::new(IpAddr::V4(addr), port))
            }
            Some(url::Host::Ipv6(addr)) => {
                // have IPv6 address already
                Some(SocketAddr::new(IpAddr::V6(addr), port))
            }
            _ => None,
        };
        ip_addr_opt
    }

    /// Attempt to resolve the hostname of a conversation's data URL to its IP address.
    fn try_resolve_data_url_host(
        &mut self,
        dns_client_opt: &mut Option<&mut DNSClient>,
        dns_timeout: u128,
    ) {
        if self.data_ip.is_some() {
            return;
        }
        if self.data_url.is_empty() {
            return;
        }
        if let Some(ipaddr) = Self::try_decode_data_url_ipaddr(&self.data_url) {
            // don't need to resolve!
            debug!(
                "{}: Resolved data URL {} to {}",
                &self, &self.data_url, &ipaddr
            );
            self.data_ip = Some(ipaddr);
            return;
        }
```

**File:** stackslib/src/net/neighbors/rpc.rs (L195-241)
```rust
    pub fn send_request(
        &mut self,
        network: &mut PeerNetwork,
        naddr: NeighborAddress,
        request: StacksHttpRequest,
    ) -> Result<(), NetError> {
        let nk = naddr.to_neighbor_key(network);
        let convo = network
            .get_neighbor_convo(&nk)
            .ok_or(NetError::PeerNotConnected(format!(
                "No authenticated conversation open to {nk} -- cannot perform HTTP request",
            )))?;
        let data_url = convo.data_url.clone();
        let data_addr = if let Some(ip) = convo.data_ip {
            ip
        } else if convo.waiting_for_dns() {
            debug!(
                "{}: have not resolved {} data URL {} yet: waiting for DNS",
                network.get_local_peer(),
                &convo,
                &data_url
            );
            return Err(NetError::WaitingForDNS);
        } else {
            debug!(
                "{}: have not resolved {} data URL {} yet, and not waiting for DNS",
                network.get_local_peer(),
                &convo,
                &data_url
            );
            return Err(NetError::PeerNotConnected(format!(
                "Have not resolved {nk} data URL {data_url} yet, and not waiting for DNS",
            )));
        };

        let event_id =
            PeerNetwork::with_network_state(network, |ref mut network, ref mut network_state| {
                PeerNetwork::with_http(network, |ref mut network, ref mut http| {
                    match http.connect_http(network_state, network, data_url, data_addr, None) {
                        Ok(event_id) => Ok(event_id),
                        Err(NetError::AlreadyConnected(event_id, _)) => Ok(event_id),
                        Err(e) => {
                            return Err(e);
                        }
                    }
                })
            })?;
```
