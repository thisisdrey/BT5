### Title
SSRF via unvalidated peer-supplied `data_url` reaching internal-network HTTP fetches in Atlas attachment sync and neighbor RPC - (File: `stackslib/src/net/atlas/download.rs`, `stackslib/src/net/neighbors/rpc.rs`, `stackslib/src/net/chat.rs`)

### Summary
Any remote P2P peer supplies its own HTTP `data_url` in the `Handshake`/`HandshakeAccept` message. This value is stored verbatim in `ConversationP2P::data_url` with no address-range validation. The private-IP check that exists in this codebase for connections derived from this same field (`PeerAddress::is_in_private_range()`) is only applied in the mempool-sync path. Other consumers of `convo.data_url` — the Atlas attachment downloader and the Nakamoto neighbor-RPC client — connect to whatever host:port the field encodes, including loopback/private/link-local addresses, causing the node to act as an SSRF proxy against its own internal network or other addresses reachable from the node.

### Finding Description
`ConversationP2P::update_from_handshake_data` sets `self.data_url = handshake_data.data_url.clone()` directly from attacker-controlled handshake data, with no validation: [1](#0-0) 

This `data_url` (or its resolved IP, `convo.data_ip`) is later used to open outbound HTTP connections in at least two places:

1. **Neighbor RPC** (`stackslib/src/net/neighbors/rpc.rs::send_request`) resolves `data_addr` straight from `convo.data_ip`/`convo.data_url` and calls `http.connect_http(...)` with no private-range check: [2](#0-1) 

2. **Atlas attachment sync** builds its peer set from `network.get_data_url(&peer)` for every outbound sync peer and issues HTTP GETs for `/v2/attachments/inv` and `/v2/attachments/{hash}` via `PeerNetwork::begin_request` → `connect_or_send_http_request`, again with no private-range check: [3](#0-2) [4](#0-3) [5](#0-4) 

By contrast, the mempool-sync code path that consumes the exact same `data_url`/resolved address explicitly guards against private targets: [6](#0-5) 

using `PeerAddress::is_in_private_range()`: [7](#0-6) 

This is an equality that should hold but does not: "any consumer of `convo.data_url` should apply the same private-range gate," but only the mempool-sync consumer does. Attachment sync and neighbor RPC treat the peer-controlled URL as trustworthy without re-validating it, letting a remote, unprivileged peer direct the node's outbound HTTP client at arbitrary internal targets (e.g. `127.0.0.1`, RFC1918 ranges, or link-local metadata-style addresses) purely by advertising that address in its handshake.

### Impact Explanation
A malicious peer that completes a normal (unprivileged, unauthenticated-until-handshake) handshake can set its `data_url` to point at an internal service (e.g. an admin HTTP API, a Redis instance speaking a subset of HTTP, or another node's non-P2P management port) reachable from the victim node. The victim will then issue GET requests (Atlas attachment/inv fetch, neighbor RPC calls) to that internal address. This is a blind/semi-blind SSRF: the node becomes a network-position confused deputy that can probe and interact with internal resources it wouldn't otherwise expose to the attacker, satisfying the "unauthorized write to state" / "network position abuse" class of impact for internal services that accept unauthenticated GET/POST requests. Severity is bounded by the fact that response contents are not directly relayed back to the attacker (unlike the classic fetch-mcp CVE where fetched content is returned verbatim), but reachability/timing signals (e.g., inclusion in Atlas reliability reports, `blocked_urls`) leak port/availability information about internal targets.

### Likelihood Explanation
Likelihood is moderate: any peer that can complete a handshake (which requires no privileged capability, just being an accepted P2P connection) can set an arbitrary `data_url`. The private-range mitigation already exists in one code path (`mempool_sync_send_query`'s `SendQuery` state gated by `network.get_connection_opts().private_neighbors`), showing that maintainers are aware of the risk for this field but did not apply the same gate to the Atlas downloader or the neighbor-RPC client, which consume the identical untrusted field.

### Recommendation
Apply the same `PeerAddress::is_in_private_range()` (respecting `connection_opts.private_neighbors`) gate at the single point where `connect_or_send_http_request`/`connect_http` resolves a `data_url` to a `SocketAddr`, rather than in each individual caller (mempool sync, atlas download, neighbor RPC). This closes the gap for all current and future consumers of `convo.data_url` in one place.

### Proof of Concept
1. Stand up a malicious peer that completes the Stacks P2P handshake with a victim node, setting `HandshakeData.data_url` to `http://127.0.0.1:<victim-admin-port>` or another internal address reachable from the victim (e.g. `http://169.254.169.254`).
2. Ensure the malicious peer is selected as an outbound Atlas sync peer (`network.get_outbound_sync_peers()`), which happens naturally once its `data_url` and services are recorded via `chat.rs::update_from_handshake_data`.
3. Observe that the Atlas downloader (`stackslib/src/net/atlas/download.rs`) issues `/v2/attachments/inv` and `/v2/attachments/{hash}` HTTP requests to the attacker-chosen internal address via `connect_or_send_http_request`, with no check against `PeerAddress::is_in_private_range()` — unlike the equivalent mempool-sync path which explicitly rejects such addresses.

### Citations

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

**File:** stackslib/src/net/atlas/download.rs (L115-124)
```rust
                let mut peers = HashMap::new();
                for peer in network.get_outbound_sync_peers() {
                    if let Some(peer_url) = network.get_data_url(&peer) {
                        let report = match self.reliability_reports.get(&peer_url) {
                            Some(report) => report.clone(),
                            None => ReliabilityReport::empty(),
                        };
                        peers.insert(peer_url, report);
                    }
                }
```

**File:** stackslib/src/net/download/epoch2x.rs (L1916-1956)
```rust
    pub fn begin_request<T: Requestable>(
        network: &mut PeerNetwork,
        dns_lookups: &HashMap<UrlString, Option<Vec<SocketAddr>>>,
        requestables: &mut VecDeque<T>,
    ) -> Option<(T, usize)> {
        loop {
            match requestables.pop_front() {
                Some(requestable) => {
                    if let Some(Some(ref sockaddrs)) = dns_lookups.get(requestable.get_url()) {
                        assert!(!sockaddrs.is_empty());

                        let peerhost = match PeerHost::try_from_url(requestable.get_url()) {
                            Some(ph) => ph,
                            None => {
                                warn!("Unparseable URL {:?}", requestable.get_url());
                                continue;
                            }
                        };

                        for addr in sockaddrs.iter() {
                            let request = requestable.make_request_type(peerhost.clone());
                            match network.connect_or_send_http_request(
                                requestable.get_url().clone(),
                                *addr,
                                request,
                            ) {
                                Ok(handle) => {
                                    debug!(
                                        "{:?}: Begin HTTP request {}",
                                        &network.local_peer, requestable
                                    );
                                    return Some((requestable, handle));
                                }
                                Err(e) => {
                                    debug!(
                                        "{:?}: Failed to connect or send HTTP request {}: {:?}",
                                        &network.local_peer, requestable, &e
                                    );
                                }
                            }
                        }
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
