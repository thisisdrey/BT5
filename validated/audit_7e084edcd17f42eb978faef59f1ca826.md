### Title
Peer-supplied `data_url` is resolved and connected to with no private/loopback/link-local range check, enabling SSRF against the node's internal network - ([File: stackslib/src/net/chat.rs])

### Summary
A remote, unauthenticated-until-handshake peer supplies its own `data_url` (advertised HTTP API endpoint) inside `HandshakeData`. The local node stores this value verbatim on the `ConversationP2P` (`self.data_url`) and later resolves it via DNS/IP parsing in `try_resolve_data_url_host`, then makes an outbound raw TCP+HTTP connection to the resolved address via `HttpPeer::connect_http` / `NetworkState::connect`. Unlike the p2p connection path (which gates `connect_peer`/`can_register_peer` on `PeerAddress::is_in_private_range()`), this HTTP "data URL" connection path performs no such range check.

### Finding Description
- A peer's advertised HTTP endpoint originates entirely from the remote party: `HandshakeData::from_local_peer` sets `data_url` from the peer's own configuration [1](#0-0) , and the receiving side stores it into the conversation state as `self.data_url` when processing the handshake (`update_from_handshake_data`, reached via `handle_handshake`) [2](#0-1) .
- To actually use that peer, the network resolves this attacker-controlled URL's host via `try_resolve_data_url_host`, taking either the literal IPv4/IPv6 address embedded in the URL or a DNS-resolved address, and stores it as `self.data_ip` with **no filtering of private, loopback, link-local, unspecified, or CGNAT ranges** [3](#0-2) .
- That resolved address is then used to open a real outbound socket: `PeerNetworkComms::send_request` (used for RPC calls like neighbor-of-neighbor HTTP lookups) and `PeerNetwork::connect_or_send_http_request` (used by the Atlas `AttachmentsDownloader` to fetch attachments/inventories from `peer.get_data_url()`) both call `http.connect_http(network_state, network, data_url, data_addr, ...)` with the peer-controlled address [4](#0-3) [5](#0-4) .
- `HttpPeer::connect_http` performs a real `NetworkState::connect(&addr, ...)` — a raw TCP connect to whatever address was supplied — without any private/loopback/link-local check, unlike the p2p connection path [6](#0-5) .
- Contrast this with the P2P (non-HTTP) connection path, which explicitly checks `neighbor_key.addrbytes.is_in_private_range()` before connecting, and rejects it by default (`private_neighbors=false`) [7](#0-6) . No analogous check exists on the `data_url`/HTTP-connect path, so this equality — "an address is only dialed if it passed the same routability/privacy gate as p2p peers" — is broken for the HTTP side.

This is the direct analog of the NocoDB bug class: the connect-target host is attacker-supplied, resolved, and dialed without any address-range validation.

### Impact Explanation
Any remote peer that completes a p2p handshake with a Stacks node (unauthenticated action — handshakes are accepted from anyone unless `disable_inbound_handshakes` is set) can set its `data_url` to `http://127.0.0.1:<port>`, `http://169.254.169.254/...` (cloud metadata), or any internal-network address/hostname. The victim node's Atlas attachment downloader and RPC subsystem will then issue real outbound HTTP requests to that address whenever it treats the attacker as an "outbound sync peer" or targets it for an RPC call, allowing the attacker to:
- Probe/interact with internal services reachable from the node's network namespace (databases, caches, internal HTTP APIs, cloud metadata endpoints).
- Use the node as a network pivot/blind SSRF proxy.

This matches the "High" bar in scope (bounded compute/read-endpoint style abuse against internal services from a read/relay flow), reachable by any unauthenticated-to-privileged peer.

### Likelihood Explanation
Likelihood is high: no privileged role, secret key, or admin access is required — merely establishing a p2p handshake (a normal, unauthenticated operation for any reachable Stacks node) and setting `data_url` to an internal address. The Atlas attachment sync loop periodically selects "outbound sync peers" and calls `network.get_data_url(&peer)` to build its peer set for HTTP inventory/attachment requests, so the malicious URL will be dialed during ordinary node operation without further attacker action.

### Recommendation
Before storing/using a peer-reported `data_url`, resolve the host and reject (or drop) the value if the resolved address is loopback, private (RFC1918/ULA), link-local, unspecified, broadcast, or CGNAT — the same check already applied to p2p neighbor addresses via `PeerAddress::is_in_private_range()`/`is_anynet()`. Apply this check both:
1. In `try_resolve_data_url_host` before setting `self.data_ip` (stackslib/src/net/chat.rs), and
2. As a final gate immediately before `HttpPeer::connect_http` is invoked (stackslib/src/net/server.rs, stackslib/src/net/neighbors/rpc.rs, stackslib/src/net/atlas/download.rs), to also guard against DNS-rebinding between resolution and connect.
Honor the node's existing `private_neighbors` configuration option consistently for this path as well, so operators who intentionally run private test networks can opt in.

### Proof of Concept
1. Stand up a malicious peer that completes the Stacks p2p `Handshake`/`HandshakeAccept` exchange with a victim node, setting `HandshakeData.data_url = "http://169.254.169.254/latest/meta-data/"` (or `http://127.0.0.1:6379/`, targeting a local service on the victim's host/pod).
2. Register a StackerDB contract / attachment reference so the victim's Atlas subsystem includes this peer as an "outbound sync peer" (`network.get_outbound_sync_peers()` / `get_data_url`), or trigger any RPC path that calls `PeerNetworkComms::send_request` against this neighbor.
3. Observe the victim node open an outbound TCP connection to the attacker-chosen internal/private address and issue an HTTP GET (e.g., `/v2/attachments/inv`) — confirmable via a listener on the internal address or via network capture showing the connect handshake succeed to a private/loopback destination, which `can_register_peer`'s `is_in_private_range()` check would have rejected on the p2p side but which the HTTP `connect_http` path does not check at all.

### Citations

**File:** stackslib/src/net/codec.rs (L606-634)
```rust
impl HandshakeData {
    pub fn from_local_peer(local_peer: &LocalPeer) -> HandshakeData {
        let (addrbytes, port) = match local_peer.public_ip_address {
            Some((ref public_addrbytes, ref port)) => (public_addrbytes.clone(), *port),
            None => (local_peer.addrbytes.clone(), local_peer.port),
        };

        // transmit the empty string if our data URL compels us to bind to the anynet address
        let data_url = if local_peer.data_url.has_routable_host() {
            local_peer.data_url.clone()
        } else if let Some(data_port) = local_peer.data_url.get_port() {
            // deduce from public IP
            UrlString::try_from(format!("http://{}", addrbytes.to_socketaddr(data_port))).unwrap()
        } else {
            // unroutable, so don't bother
            UrlString::try_from("").unwrap()
        };

        HandshakeData {
            addrbytes,
            port,
            services: local_peer.services,
            node_public_key: StacksPublicKeyBuffer::from_public_key(
                &Secp256k1PublicKey::from_private(&local_peer.private_key),
            ),
            expire_block_height: local_peer.private_key_expire,
            data_url,
        }
    }
```

**File:** stackslib/src/net/chat.rs (L1214-1251)
```rust
    fn handle_handshake(
        &mut self,
        network: &mut PeerNetwork,
        message: &mut StacksMessage,
        authenticated: bool,
        ibd: bool,
    ) -> Result<(Option<StacksMessage>, bool), net_error> {
        if !authenticated && self.connection.options.disable_inbound_handshakes {
            debug!("{:?}: blocking inbound unauthenticated handshake", &self);
            return Ok((None, true));
        }

        let res =
            self.validate_handshake(network.get_local_peer(), network.get_chain_view(), message);
        match res {
            Ok(_) => {}
            Err(net_error::InvalidHandshake) => {
                let reject = StacksMessage::from_chain_view(
                    self.version,
                    self.network_id,
                    network.get_chain_view(),
                    StacksMessageType::HandshakeReject,
                );
                debug!("{:?}: invalid handshake", &self);
                return Ok((Some(reject), true));
            }
            Err(e) => {
                return Err(e);
            }
        };

        let handshake_data = match message.payload {
            StacksMessageType::Handshake(ref mut data) => data.clone(),
            _ => panic!("Message is not a handshake"),
        };

        let old_pubkey_opt = self.connection.get_public_key();
        let updated = self.update_from_handshake_data(&message.preamble, &handshake_data)?;
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

**File:** stackslib/src/net/atlas/download.rs (L107-146)
```rust
        let ongoing_fsm = match self.ongoing_batch.take() {
            Some(batch) => batch,
            None => {
                if self.priority_queue.is_empty() || !self.has_ready_batches() {
                    // Nothing to do!
                    return Ok((vec![], vec![]));
                }

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
                if peers.is_empty() {
                    warn!("Atlas: could not get a peer to sync with");
                    // Nothing can be done!
                    return Ok((vec![], vec![]));
                }

                let attachments_batch = match self.pop_next_ready_batch() {
                    Some(ready_batch) => ready_batch,
                    None => {
                        // unreachable
                        warn!("BUG: Atlas; no batch ready although logic checking for ready batches found one");
                        return Ok((vec![], vec![]));
                    }
                };

                let ctx = AttachmentsBatchStateContext::new(
                    attachments_batch,
                    peers,
                    &network.connection_opts,
                );
                AttachmentsBatchStateMachine::new(ctx)
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
