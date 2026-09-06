### Title
Node makes outbound HTTP requests to peer-controlled `data_url` without private-IP filtering, enabling SSRF via StackerDB sync / neighbor RPC - (File: stackslib/src/net/neighbors/rpc.rs)

### Summary
The Handshake message lets any remote peer supply an arbitrary `data_url` string [1](#0-0) , which is stored verbatim on the conversation via `update_from_handshake_data` [2](#0-1)  and later resolved to a `SocketAddr` and cached as `data_ip` [3](#0-2) . Several subsystems then automatically open outbound HTTP connections to this attacker-chosen `data_url`/`data_ip` as part of normal node operation (StackerDB sync, neighbor RPC, mempool sync, attachment/block download), but only one of these paths (mempool sync) filters out private/internal IP ranges before connecting.

### Finding Description
`PeerNetwork::mempool_sync_state_machine` explicitly checks that a resolved `data_url` address is not in a private IP range before issuing the query, unless `private_neighbors` is enabled: [4](#0-3) 

However, `NeighborRPC::send_request`, which is used by StackerDB sync (`stackslib/src/net/stackerdb/sync.rs`) and other neighbor-RPC-driven flows to fetch data (e.g. `StackerDBGetChunkData` requests), connects directly to the neighbor's cached `data_ip`/`data_url` with no equivalent private-range check: [5](#0-4) 

The `data_ip` used here is populated purely from the peer-supplied Handshake `data_url` via `try_decode_data_url_ipaddr` / DNS resolution in `chat.rs`, with no filtering against loopback, link-local, or RFC1918 addresses: [6](#0-5) 

Similarly, `connect_or_send_http_request` / `HttpPeer::connect_http` (used by the block/microblock/attachment downloader in `download/epoch2x.rs` and `atlas/download.rs`) opens a raw TCP connection to whatever `SocketAddr` was resolved from the peer-supplied URL, again with no private-range check at this layer: [7](#0-6) [8](#0-7) 

This breaks the implicit equality that the mempool-sync code enforces ("only ever originate outbound requests to non-private addresses unless explicitly configured otherwise") — that invariant is applied inconsistently across the codebase. Because `data_url` is fully attacker-controlled (any peer that completes a Handshake, including inbound, unauthenticated-until-verified peers), a malicious peer can set it to `http://127.0.0.1:<port>` or an internal RFC1918 address, and cause the victim node to originate genuine HTTP requests toward that target whenever StackerDB sync, attachment or block download decide to fetch data from that "neighbor."

### Impact Explanation
This matches the "network-wide propagation of forged data" / SSRF-class report only partially — the more concrete effect here is that the node acts as a confused deputy, sending crafted HTTP requests (GET `/v2/attachments/...`, POST `/v2/stackerdb/.../chunks`, GET `/v3/sortitions`, etc.) to a destination chosen by a remote, unprivileged peer, including addresses on the node operator's internal/local network (e.g., an admin API bound to loopback, or other internal services). This does not by itself corrupt consensus, but it does provide a remote, unauthenticated vector to make a node probe/interact with internal-only network endpoints, which is the essential SSRF primitive from the source advisory.

### Likelihood Explanation
Reaching this path requires only that a remote peer complete a P2P Handshake and become a known "neighbor" with an attacker-chosen `data_url` — the private-IP guard exists in exactly one call site (mempool sync) and is absent from `NeighborRPC::send_request`, the StackerDB sync's primary chunk-fetch path, and `connect_or_send_http_request`. Because StackerDB sync is a normal, continuously-running background process, this can be triggered passively rather than requiring precise timing.

### Recommendation
Apply the same private/loopback-address filtering used in `mempool_sync_state_machine` (`stackslib/src/net/mempool/mod.rs:488-498`) uniformly at the point where `data_ip`/`data_url` is resolved into a `SocketAddr` (e.g., in `chat.rs`'s DNS/IP resolution and in `NeighborRPC::send_request` / `connect_or_send_http_request`), gated by the existing `private_neighbors` config flag, rather than relying on each individual subsystem to remember to add the check.

### Proof of Concept
Not independently reproducible from static analysis alone (would require running two nodes with `private_neighbors=false`, one crafted to advertise `data_url = "http://127.0.0.1:<internal-port>"` in its Handshake, and observing that the victim's StackerDB sync or neighbor RPC path initiates an HTTP connection to that address). I was not able to execute this in the current environment; the finding rests on the code-level inconsistency identified above.

### Citations

**File:** stackslib/src/net/mod.rs (L1040-1048)
```rust
#[derive(Debug, Clone, PartialEq)]
pub struct HandshakeData {
    pub addrbytes: PeerAddress,
    pub port: u16,
    pub services: u16, // bit field representing services this node offers
    pub node_public_key: StacksPublicKeyBuffer,
    pub expire_block_height: u64, // burn block height after which this node's key will be revoked,
    pub data_url: UrlString,
}
```

**File:** stackslib/src/net/chat.rs (L1131-1147)
```rust
    pub fn update_from_handshake_data(
        &mut self,
        preamble: &Preamble,
        handshake_data: &HandshakeData,
    ) -> Result<bool, net_error> {
        let pubk = handshake_data
            .node_public_key
            .to_public_key()
            .map_err(|e| net_error::DeserializeError(e.into()))?;

        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();
```

**File:** stackslib/src/net/chat.rs (L2805-2913)
```rust
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

        let Some(dns_client) = dns_client_opt else {
            return;
        };
        if get_epoch_time_ms() < self.dns_deadline {
            return;
        }
        if let Some(dns_request) = self.dns_request.take() {
            // perhaps resolution completed?
            match dns_client.poll_lookup(&dns_request.host, dns_request.port) {
                Ok(query_result_opt) => {
                    // just take one of the addresses, if there are any
                    self.data_ip =
                        query_result_opt.and_then(|query_result| match query_result.result {
                            Ok(mut ips) => ips.pop(),
                            Err(e) => {
                                warn!(
                                    "{}: Failed to resolve data URL {}: {:?}",
                                    self, &self.data_url, &e
                                );

                                // don't try again
                                self.dns_deadline = u128::MAX;
                                None
                            }
                        });
                    if let Some(ip) = self.data_ip.as_ref() {
                        debug!("{}: Resolved data URL {} to {}", &self, &self.data_url, &ip);
                    } else {
                        info!(
                            "{}: Failed to resolve URL {}: no IP addresses found",
                            &self, &self.data_url
                        );
                    }
                    // don't try again
                    self.dns_deadline = u128::MAX;
                }
                Err(e) => {
                    warn!("DNS lookup failed on {}: {:?}", &self.data_url, &e);

                    // don't try again
                    self.dns_deadline = u128::MAX;
                }
            }
        }

        // need to begin resolution
        // NOTE: should always succeed, since a UrlString shouldn't decode unless it's a valid URL or the empty string
        let Ok(url) = self.data_url.parse_to_block_url() else {
            return;
        };
        let port = match url.port_or_known_default() {
            Some(p) => p,
            None => {
                warn!("Unsupported URL {:?}: unknown port", &url);

                // don't try again
                self.dns_deadline = u128::MAX;
                return;
            }
        };
        let ip_addr_opt = match url.host() {
            Some(url::Host::Domain(domain)) => {
                // need to resolve a DNS name
                let deadline = get_epoch_time_ms().saturating_add(dns_timeout);
                if let Err(e) = dns_client.queue_lookup(domain, port, deadline) {
                    debug!("Failed to queue DNS resolution of {}: {:?}", &url, &e);
                    return;
                }
                self.dns_request = Some(DNSRequest::new(domain.to_string(), port, 0));
                self.dns_deadline = deadline;
                None
            }
            Some(url::Host::Ipv4(addr)) => {
                // have IPv4 address already
                Some(SocketAddr::new(IpAddr::V4(addr), port))
            }
            Some(url::Host::Ipv6(addr)) => {
                // have IPv6 address already
                Some(SocketAddr::new(IpAddr::V6(addr), port))
            }
            None => {
                warn!("Unsupported URL {:?}", &url);

                // don't try again
                self.dns_deadline = u128::MAX;
                return;
            }
        };
        self.data_ip = ip_addr_opt;
        if let Some(ip) = self.data_ip.as_ref() {
            debug!("{}: Resolved data URL {} to {}", &self, &self.data_url, &ip);
        }
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

**File:** stackslib/src/net/neighbors/rpc.rs (L191-241)
```rust
    /// Send an HTTP request to the given neighbor's HTTP endpoint.
    /// The peer must already be connected and authenticated via the p2p network.
    /// Returns Ok(()) if we successfully queue the request.
    /// Returns Err(..) if we fail to connect to the remote peer for some reason.
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

**File:** stackslib/src/net/httpcore.rs (L1877-1911)
```rust
    pub fn connect_or_send_http_request(
        &mut self,
        data_url: UrlString,
        addr: SocketAddr,
        request: StacksHttpRequest,
    ) -> Result<usize, NetError> {
        PeerNetwork::with_network_state(self, |ref mut network, ref mut network_state| {
            PeerNetwork::with_http(network, |ref mut network, ref mut http| {
                match http.connect_http(
                    network_state,
                    network,
                    data_url.clone(),
                    addr,
                    Some(request.clone()),
                ) {
                    Ok(event_id) => Ok(event_id),
                    Err(NetError::AlreadyConnected(event_id, _)) => {
                        if let (Some(ref mut convo), Some(ref mut socket)) =
                            http.get_conversation_and_socket(event_id)
                        {
                            convo.send_request(request)?;
                            HttpPeer::saturate_http_socket(socket, convo)?;
                            Ok(event_id)
                        } else {
                            debug!("HTTP failed to connect to {data_url}, {addr:?}");
                            Err(NetError::PeerNotConnected(format!(
                                "HTTP failed to connect to {data_url}, {addr:?}",
                            )))
                        }
                    }
                    Err(e) => Err(e),
                }
            })
        })
    }
```
