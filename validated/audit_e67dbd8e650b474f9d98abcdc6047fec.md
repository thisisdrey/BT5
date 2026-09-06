### Title
SSRF-analog: peer-supplied `data_url` drives unauthenticated outbound HTTP requests without private-IP filtering in the Nakamoto tenure/block downloader - (File: `stackslib/src/net/neighbors/rpc.rs`, `stackslib/src/net/download/epoch2x.rs`)

### Summary
A remote, unauthenticated peer can advertise an arbitrary `data_url` in its `HandshakeData`. This value is stored verbatim on the conversation (`convo.data_url`) and later used by the node to issue outbound HTTP requests (block/tenure downloads, mempool sync, StackerDB sync) to whatever host/port the string encodes — it need not match the actual TCP peer the node is talking to. The mempool-sync code path (`stackslib/src/net/mempool/mod.rs`) explicitly guards this by rejecting resolved addresses in a private IP range unless `private_neighbors` is enabled, but the equivalent guard is absent from the Nakamoto tenure/inventory RPC download path (`stackslib/src/net/neighbors/rpc.rs::send_request`) and the epoch2x block/microblock downloader (`stackslib/src/net/download/epoch2x.rs::begin_request` / `connect_or_send_http_request`). This breaks the equality "outbound requests are only ever made to the peer we actually connected to" and reproduces the SSRF bug class from the report (URL-controlled outbound fetch with no destination validation).

### Finding Description
- `HandshakeData.data_url` is fully attacker-controlled and deserialized with no destination validation beyond basic URL syntax: [1](#0-0) 
- On receipt of a handshake, the value is copied directly into the conversation state used for all future HTTP requests to that peer: [2](#0-1) 
- `NeighborRPC::send_request` (used for Nakamoto tenure/inventory downloads) resolves `convo.data_url` and connects to it via `connect_or_send_http_request` with no check on whether the resolved address is private/internal: [3](#0-2) 
- The generic epoch2x downloader `begin_request` similarly consumes a peer-supplied `data_url`, resolves DNS, and calls `connect_or_send_http_request` for every resolved socket address with no IP-range filtering: [4](#0-3) 
- By contrast, the mempool-sync state machine (which uses the same peer-supplied `data_url` field) explicitly checks the resolved address against private IP ranges before issuing the request, and skips it unless `private_neighbors` is configured: [5](#0-4) 

This shows the private-IP/SSRF mitigation is only applied inconsistently — one outbound-URL consumer (mempool sync) guards against internal-address targeting, while the tenure/block/inventory downloader and the low-level `connect_or_send_http_request`/`begin_request` machinery do not. Since `data_url` is not required to match the actual socket address the peer connected from, a malicious peer can set it to point at an internal service (e.g., `http://127.0.0.1:<port>/...`, a cloud metadata endpoint, or another node's private RPC/admin port) and the victim node will make outbound HTTP GET/POST requests there whenever it attempts to sync blocks, tenures, or inventories with that peer.

### Impact Explanation
This matches the "High" impact bucket: it allows an unauthenticated/low-privilege remote peer to cause the victim node to make network requests to arbitrary internal or restricted endpoints reachable from the node's network position, using the node itself as a proxy. Depending on internal topology this can probe or interact with internal-only services (management APIs, metadata services, other private RPC ports) that are not otherwise reachable by the attacker, and can be trivially repeated as long as the node keeps a p2p handshake alive with the attacker's node.

### Likelihood Explanation
Likelihood is High: any peer that can complete a p2p handshake (no special privilege required — handshakes are processed even before full authentication in `handle_unauthenticated_control_message`, [6](#0-5) ) can set an arbitrary `data_url`, and the tenure/block download machinery will use it during ordinary block/tenure sync activity without any destination check.

### Recommendation
Apply the same private/internal address filtering used in mempool sync (`stackslib/src/net/mempool/mod.rs`, the `is_in_private_range`/`private_neighbors` check) uniformly to every consumer of peer-supplied `data_url`, in particular `NeighborRPC::send_request` (`stackslib/src/net/neighbors/rpc.rs`) and the epoch2x downloader's `begin_request`/`connect_or_send_http_request` path (`stackslib/src/net/download/epoch2x.rs`, `stackslib/src/net/httpcore.rs`). Reject or gate (behind `private_neighbors`) any resolved address that falls in loopback/link-local/private ranges, applied consistently at the point where any `data_url`-derived socket address is used to open an outbound HTTP connection.

### Proof of Concept
1. Attacker node completes a p2p handshake with the victim, sending a `HandshakeData` with `data_url = "http://127.0.0.1:<internal-port>/"` (or any internal-only address), per the codec's unrestricted `UrlString` field (`stackslib/src/net/codec.rs:648-668`).
2. Victim's `ConversationP2P::update_from_handshake_data` stores this as `convo.data_url` (`stackslib/src/net/chat.rs:1147`) without validating that it matches the peer's actual socket address.
3. When the victim later attempts to download tenure/inventory data from this peer via `NeighborRPC::send_request` (`stackslib/src/net/neighbors/rpc.rs:195-241`) or via the epoch2x downloader (`stackslib/src/net/download/epoch2x.rs:1916-1956`), it resolves and connects to the attacker-chosen address with no private-IP check, unlike the equivalent mempool-sync path (`stackslib/src/net/mempool/mod.rs:488-498`), causing an outbound request to the internal target.

### Citations

**File:** stackslib/src/net/codec.rs (L648-668)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<HandshakeData, codec_error> {
        let addrbytes: PeerAddress = read_next(fd)?;
        let port: u16 = read_next(fd)?;
        if port == 0 {
            return Err(codec_error::DeserializeError(
                "Invalid handshake data: port is 0".to_string(),
            ));
        }

        let services: u16 = read_next(fd)?;
        let node_public_key: StacksPublicKeyBuffer = read_next(fd)?;
        let expire_block_height: u64 = read_next(fd)?;
        let data_url: UrlString = read_next(fd)?;
        Ok(HandshakeData {
            addrbytes,
            port,
            services,
            node_public_key,
            expire_block_height,
            data_url,
        })
```

**File:** stackslib/src/net/chat.rs (L1141-1147)
```rust
        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();
```

**File:** stackslib/src/net/chat.rs (L2611-2624)
```rust
        // only thing we'll take right now is a handshake, as well as handshake
        // accept/rejects, nacks, and NAT holepunches
        //
        // Anything else will be nack'ed -- the peer will first need to handshake.
        let mut consume = false;
        let solicited = self.connection.is_solicited(msg);
        let reply_opt = match msg.payload {
            StacksMessageType::Handshake(_) => {
                monitoring::increment_msg_counter("p2p_unauthenticated_handshake".to_string());
                debug!("{:?}: Got unauthenticated Handshake", &self);
                let (reply_opt, handled) = self.handle_handshake(network, msg, false, ibd)?;
                consume = handled;
                Ok(reply_opt)
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
