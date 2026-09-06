## #Vulnerability found

### Title
Server-Side Request Forgery via unvalidated peer-supplied `data_url` in P2P HTTP-fetch paths — (File: `stackslib/src/net/chat.rs`, `stackslib/src/net/httpcore.rs`, `stackslib/src/net/neighbors/rpc.rs`, `stackslib/src/net/download/epoch2x.rs`)

### Summary
A remote, unauthenticated peer can advertise an arbitrary `data_url` (including link-local/loopback/private addresses such as `169.254.169.254`) in its P2P `Handshake` message. This value is stored verbatim and later resolved and dialed by the node's HTTP-fetch machinery for block/tenure downloads, StackerDB chunk sync, and neighbor-walk HTTP calls — with **no private-IP/metadata-endpoint filtering** on those paths. Only the unrelated mempool-sync code path enforces such a check, exposing the inconsistency.

### Finding Description
When a peer handshakes, `ConversationP2P::update_from_handshake_data` copies the peer-supplied `data_url` directly into `self.data_url` without any validation of the destination: [1](#0-0) 

`HandshakeData` deserialization only rejects `port == 0`; the `data_url` string itself is unconstrained: [2](#0-1) 

When the node needs to talk to this peer's HTTP endpoint (e.g., to resolve DNS and open a connection), `resolve_data_url` accepts any IPv4/IPv6 literal — including `169.254.169.254` or `127.0.0.1` — without checking `is_in_private_range()`: [3](#0-2) 

The resolved address is then dialed by multiple independent call sites that never re-validate it:

- Generic HTTP connect helper used by StackerDB/relay flows: [4](#0-3) 

- Low-level socket connect: [5](#0-4) 

- Nakamoto tenure/inv/StackerDB neighbor RPC send path: [6](#0-5) 

- Legacy block/microblock downloader's request dispatch: [7](#0-6) 

In contrast, the mempool-sync state machine is the *only* consumer of a peer-supplied data URL that explicitly guards against private-range targets: [8](#0-7) 

This confirms the private-IP/SSRF guard is a known, intentional control in this codebase (`PeerAddress::is_in_private_range()`, gated by `connection_opts.private_neighbors`) — it is simply missing from the block-download, tenure-download, and StackerDB-sync HTTP-fetch code paths, breaking the invariant that "no outbound P2P/RPC HTTP request may target a private/link-local address unless `private_neighbors` is explicitly enabled."

Note that `stackerdb_hint_replicas` config parsing does filter private ranges (`stackslib/src/net/stackerdb/config.rs`), and neighbor-walk address correction (`walk.rs:693-703`) only rewrites a private *handshake-reported connect address*, but neither of these mechanisms touches the `data_url` string used for actual HTTP data-plane requests.

### Impact Explanation
Any remote, unprivileged peer that can open a P2P connection to a node (no admin key, no signer role required — just a self-signed handshake key) can set `data_url` to an internal/link-local target. Whenever the local node performs a legitimate protocol action against that peer — StackerDB chunk fetch/push scheduling, Nakamoto tenure/inv download, or legacy block/microblock download — it will issue an outbound HTTP request to the attacker-chosen address from the node's own network position. Because these peer records (with their advertised `data_url`) are also persisted in `PeerDB` and gossiped through neighbor-walk (`HandshakeAcceptData`/`StackerDBHandshakeData`), the forged `data_url` can propagate to other nodes in the network, causing many independent nodes to probe their own internal services/cloud metadata endpoints on the attacker's behalf — an unauthenticated SSRF with network-wide reach, matching the "network-wide propagation of forged data" / internal-network-disclosure impact class.

### Likelihood Explanation
High. No signer role, no admin token, and no special protocol privilege is required — a bare P2P handshake from any TCP client can supply the malicious `data_url`. The vulnerable request paths (StackerDB sync scheduling, tenure/inv download, legacy block download) are all triggered automatically by normal node operation once a peer is connected, requiring no further attacker interaction.

### Recommendation
Apply the same `PeerAddress::is_in_private_range()` (and `connection_opts.private_neighbors`) gate used in `mempool_sync_send_query`/`mempool_sync_pass` to every outbound HTTP dial derived from a peer-supplied `data_url`, specifically: `PeerNetwork::connect_or_send_http_request` / `HttpPeer::connect_http`, `NeighborRPC::send_request`, and the legacy `BlockDownloader::begin_request` path. Centralizing this check inside `connect_http`/`connect_or_send_http_request` (rather than re-implementing it per caller) would close all current and future call sites in one place.

### Proof of Concept
1. Run a malicious peer node/script that completes a Stacks P2P handshake with the target node, supplying `HandshakeData.data_url = "http://169.254.169.254/"` (or `http://127.0.0.1:<internal-port>/`).
2. Wait for the target to schedule a StackerDB chunk fetch/push, a Nakamoto tenure/inv download, or (pre-Nakamoto) a block/microblock download against this peer — these are triggered automatically as part of normal sync (`stackslib/src/net/stackerdb/sync.rs`, `stackslib/src/net/download/nakamoto/tenure_downloader.rs`, `stackslib/src/net/download/epoch2x.rs`).
3. Observe (e.g., via network capture or a local metadata-service honeypot standing in for `169.254.169.254`) that the target node issues an outbound HTTP request to the attacker-controlled/internal address, confirming the SSRF — none of these paths perform the `is_in_private_range()` check that `mempool_sync_send_query` performs.

### Citations

**File:** stackslib/src/net/chat.rs (L1140-1147)
```rust

        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();
```

**File:** stackslib/src/net/chat.rs (L2882-2913)
```rust
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

**File:** stackslib/src/net/codec.rs (L648-669)
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

**File:** stackslib/src/net/server.rs (L118-150)
```rust
    pub fn connect_http(
        &mut self,
        network_state: &mut NetworkState,
        network: &PeerNetwork,
        data_url: UrlString,
        addr: SocketAddr,
        request: Option<StacksHttpRequest>,
    ) -> Result<usize, net_error> {
        if let Some(event_id) = self.find_free_conversation(&data_url) {
            let http_nk = NeighborKey {
                peer_version: network.burnchain.peer_version,
                network_id: network.local_peer.network_id,
                addrbytes: PeerAddress::from_socketaddr(&addr),
                port: addr.port(),
            };
            return Err(net_error::AlreadyConnected(event_id, http_nk));
        }

        let sock = NetworkState::connect(
            &addr,
            network.connection_opts.socket_send_buffer_size,
            network.connection_opts.socket_recv_buffer_size,
        )?;
        let hint_event_id = network_state.next_event_id()?;
        let next_event_id =
            network_state.register(self.http_server_handle, hint_event_id, &sock)?;

        self.connecting.insert(
            next_event_id,
            (sock, Some(data_url), request, get_epoch_time_secs()),
        );
        Ok(next_event_id)
    }
```

**File:** stackslib/src/net/neighbors/rpc.rs (L191-228)
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
