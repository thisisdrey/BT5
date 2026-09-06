### Title
Unauthenticated SSRF via unvalidated peer-supplied `data_url` in Nakamoto neighbor RPC — ([File: stackslib/src/net/neighbors/rpc.rs])

### Summary
Any remote peer that completes a normal (unprivileged) P2P handshake can set an arbitrary `data_url` in its `HandshakeData`. That value is later used verbatim to open outbound HTTP connections from the victim node via `NeighborRPC::send_request`/`get_peer_host`, with no check that the resolved address is non-private/non-local — unlike the equivalent mempool-sync code path, which explicitly filters private-range addresses.

### Finding Description
`HandshakeData.data_url` is a fully attacker-controlled `UrlString` sent as part of the handshake and copied verbatim into the conversation state with no validation: [1](#0-0) 

The wire format places no constraint on its value beyond being a parseable URL: [2](#0-1) 

When the node resolves this URL to an IP (via literal IP or DNS), it stores the resulting `data_ip` without any private/loopback/link-local filtering: [3](#0-2) 

`NeighborRPC::get_peer_host` and `NeighborRPC::send_request` — used by the Nakamoto tenure/StackerDB download machinery (`stackslib/src/net/download/nakamoto/tenure_downloader.rs`) — then use this attacker-supplied host/IP directly to open an outbound TCP+HTTP connection, with no `is_in_private_range` check anywhere in the call path: [4](#0-3) 

This connects through `PeerNetwork::connect_or_send_http_request` → `HttpPeer::connect_http`, which likewise performs no private-range validation before dialing the resolved `SocketAddr`: [5](#0-4) [6](#0-5) 

By contrast, the mempool-sync subsystem, which uses the exact same peer-advertised `data_url`, explicitly rejects private-range destinations unless `private_neighbors` is enabled: [7](#0-6) 

This asymmetry shows the private-range check is a known, intended mitigation for this exact attacker-controlled input — it is simply missing from the `NeighborRPC` HTTP-request path used by newer (Nakamoto) download logic. The P2P-level private-range gate in `can_register_peer` only protects the peer's own advertised P2P `NeighborKey` address, not the independent `data_url` field: [8](#0-7) 

`is_in_private_range` itself also does not cover the `169.254.0.0/16` link-local range (e.g. cloud metadata endpoints), a secondary gap even where the check does exist: [9](#0-8) 

### Impact Explanation
A node that completes a public, unprivileged handshake (any peer can do this — no signer key, no admin role, no StackerDB membership required) can force the victim node to issue arbitrary outbound HTTP requests to attacker-chosen hosts, including the victim's own loopback services, RFC1918-internal hosts, or link-local metadata endpoints, whenever the victim's Nakamoto tenure-download / neighbor-RPC subsystem tries to fetch data from that peer. This is a classic SSRF (CWE-918): it can be used to port-scan internal infrastructure reachable from the node, and depending on what's listening on the forged target (e.g., another local RPC service, a cloud metadata service), it may leak information back into behavior observable by the attacker (timing/response codes) — a bounded-impact information-disclosure primitive reachable with a handful of unauthenticated messages.

### Likelihood Explanation
Likelihood is high for triggering the outbound connection: setting `data_url` is a normal, unauthenticated part of the P2P handshake protocol, and the tenure-downloader/`NeighborRPC` logic will use it whenever the victim wants to fetch data from that neighbor (which happens routinely during normal chain sync). The severity of the resulting SSRF depends on what internal services are reachable from the node's network position, which is environment-dependent — hence Medium/High rather than Critical.

### Recommendation
Add a private/loopback/link-local range check (extending `is_in_private_range` to also cover `169.254.0.0/16`) at the point where peer-supplied `data_url`/resolved IPs are used to open outbound HTTP connections in `NeighborRPC` (`get_peer_host`/`send_request`) and `PeerNetwork::connect_or_send_http_request`/`HttpPeer::connect_http`, mirroring the guard already present in `mempool_sync_send_query`. Respect the existing `private_neighbors` configuration flag consistently across all of these code paths.

### Proof of Concept
1. Run a normal Stacks node A that is reachable and completes P2P handshakes with the victim node V (no special privileges required).
2. Configure node A's handshake `data_url` (the field advertised in `HandshakeData`, serialized per `stackslib/src/net/codec.rs`) to point at an internal target, e.g. `http://127.0.0.1:<victim-local-port>/...` or `http://169.254.169.254/latest/meta-data/`.
3. Cause V to select A as a download source for Nakamoto tenure data (normal chain-sync behavior once A is a known/authenticated neighbor).
4. Observe that V's `NeighborRPC::send_request` (`stackslib/src/net/neighbors/rpc.rs`) resolves and connects directly to the forged internal address via `connect_or_send_http_request`/`connect_http`, with no private-range check — unlike the equivalent mempool-sync path, which would reject the same address.

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

**File:** stackslib/src/net/chat.rs (L2805-2865)
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
```

**File:** stackslib/src/net/codec.rs (L637-669)
```rust
impl StacksMessageCodec for HandshakeData {
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), codec_error> {
        write_next(fd, &self.addrbytes)?;
        write_next(fd, &self.port)?;
        write_next(fd, &self.services)?;
        write_next(fd, &self.node_public_key)?;
        write_next(fd, &self.expire_block_height)?;
        write_next(fd, &self.data_url)?;
        Ok(())
    }

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

**File:** stackslib/src/net/neighbors/rpc.rs (L182-241)
```rust
    /// Find the PeerHost to use when creating a Stacks HTTP request.
    /// Returns Some(host) if we're connected and authenticated to this peer
    /// Returns None otherwise.
    pub fn get_peer_host(network: &PeerNetwork, addr: &NeighborAddress) -> Option<PeerHost> {
        let nk = addr.to_neighbor_key(network);
        let convo = network.get_neighbor_convo(&nk)?;
        PeerHost::try_from_url(&convo.data_url)
    }

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

**File:** stackslib/src/net/server.rs (L114-150)
```rust
    /// Connect to a new remote HTTP endpoint, given the data URL and a (resolved) socket address to
    /// its origin.  Once connected, optionally send the given request.
    /// Idempotent -- will not re-connect if already connected and there is a free conversation channel open
    /// (will return Error::AlreadyConnected with the event ID)
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

**File:** stackslib/src/net/p2p.rs (L1877-1924)
```rust
    /// Check to see if we can register the given socket
    /// * we can't have registered this neighbor already
    /// * if this is inbound, we can't add more than self.num_clients
    pub fn can_register_peer(
        &mut self,
        neighbor_key: &NeighborKey,
        outbound: bool,
    ) -> Result<(), net_error> {
        // don't talk to our bind address
        if self.is_bound(neighbor_key) {
            debug!(
                "{:?}: do not register myself at {:?}",
                &self.local_peer, neighbor_key
            );
            return Err(net_error::Denied);
        }

        // denied?
        if PeerDB::is_peer_denied(
            self.peerdb.conn(),
            neighbor_key.network_id,
            &neighbor_key.addrbytes,
            neighbor_key.port,
        )? {
            info!(
                "{:?}: Peer {:?} is denied; dropping",
                &self.local_peer, neighbor_key
            );
            return Err(net_error::Denied);
        }

        // already connected?
        if let Some(event_id) = self.get_event_id(neighbor_key) {
            debug!(
                "{:?}: already connected to {:?} on event {}",
                &self.local_peer, neighbor_key, event_id
            );
            return Err(net_error::AlreadyConnected(event_id, neighbor_key.clone()));
        }

        // unroutable?
        if !self.connection_opts.private_neighbors && neighbor_key.addrbytes.is_in_private_range() {
            debug!("{:?}: Peer {:?} is in private range and we are configured to drop private neighbors",
                  &self.local_peer,
                  neighbor_key
            );
            return Err(net_error::Denied);
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
