This confirms the analog. `parse_to_block_url` in `stackslib/src/util_lib/strings.rs` validates scheme, credentials, query, and fragment for a peer-supplied `data_url`, but it never checks that the resolved host is a public/non-private address. The `HandshakeData::data_url` field is fully attacker-controlled (any remote, unprivileged peer that performs a handshake), decoded via `consensus_deserialize` with no address-scope restriction, and stored via `ConversationHttp::update_from_handshake_data` at [1](#0-0) . This is in clear contrast to the StackerDB `hint-replicas` contract-fed addresses, which explicitly reject private-range IPs via `eval_hint_replicas` at [2](#0-1) .

### Title
Handshake-supplied `data_url` is never checked against private/loopback ranges, letting a remote peer make the node issue outbound HTTP requests to internal targets (SSRF) - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationHttp` accepts an arbitrary `data_url` from any handshaking peer's `HandshakeData` and later resolves and connects to it to fetch StackerDB chunks, mempool data, and other RPC responses — with no filtering of private, loopback, or link-local addresses, unlike the equivalent StackerDB `hint-replicas` path which explicitly filters such addresses.

### Finding Description
`HandshakeData.data_url` is a `UrlString` deserialized directly off the wire with no restriction on host [3](#0-2) . When a handshake (or handshake-accept) is received, `update_from_handshake_data` copies this value verbatim into the conversation's `self.data_url` [1](#0-0) .

Later, `try_resolve_data_url_host`/`try_decode_data_url_ipaddr` parse this URL (via `UrlString::parse_to_block_url`) and resolve it to a concrete `SocketAddr`, which becomes `self.data_ip` [4](#0-3) . `parse_to_block_url` only validates scheme (`http`/`https`), absence of embedded credentials, and absence of query/fragment — it performs no check that the resolved host is a routable, non-private address [5](#0-4) .

This `data_ip`/`data_url` pair is subsequently used by `NeighborRPC::send_request` and `PeerNetwork::connect_or_send_http_request` to open a real outbound TCP connection and issue an HTTP request from the victim node to the address the remote peer supplied [6](#0-5) [7](#0-6) . These outbound requests fire from mempool sync (`mempool_sync_send_query`) [8](#0-7)  and StackerDB replica sync (`connect_begin`/`neighbor_session_begin`) [9](#0-8) , both of which are driven automatically for any known/handshaked peer, not just operator-configured ones.

This is precisely the same bug class as the CVE — a wire-supplied value is used to steer an authenticated component's own outbound request without checking whether the destination is internal/private. The codebase already recognizes this exact risk elsewhere: `eval_hint_replicas`, which parses StackerDB `hint-replicas` addresses from a smart contract, explicitly drops any `peer_addr.is_in_private_range()` entry [2](#0-1) . No equivalent filter exists on the handshake `data_url` path, breaking the intended equality "peer-advertised data endpoint == externally reachable peer" — an attacker can advertise `data_url = http://127.0.0.1:<port>/...` or any RFC1918 address and have the victim node itself connect to it.

### Impact Explanation
Any remote, unauthenticated peer that can complete a p2p handshake (a normal, permissionless action) can set `data_url` to point at `127.0.0.1`, another RFC1918 address, or a cloud metadata endpoint reachable from the node's network. The victim's own StackerDB sync and mempool sync logic will then open outbound HTTP connections to that address and read the response status/body length to some extent — enabling internal port scanning and probing of otherwise-unreachable local services from the perspective of the attacker (classic SSRF), matching the "High" tier: bounded compute/read side effects through an internal-facing read path reachable by unprivileged peers.

### Likelihood Explanation
No special privileges are required — this is triggered purely by performing a normal Handshake with a public Stacks node and choosing an internal-looking `data_url`. It requires no key theft, no consensus manipulation, and no non-default configuration, only the existing handshake and periodic sync logic already run by every full node.

### Recommendation
Reject or degrade `data_url` values whose resolved host is a private, loopback, link-local, or otherwise non-routable address, mirroring the check already present in `eval_hint_replicas` (`PeerAddress::is_in_private_range`). This check should be applied both when storing `self.data_url` in `update_from_handshake_data` and/or at DNS-resolution time in `try_resolve_data_url_host`, before any outbound connection is attempted.

### Proof of Concept
1. Stand up a malicious peer that performs a valid Handshake against a target Stacks node, using `HandshakeData { data_url: "http://127.0.0.1:<internal_port>/", .. }` (or an RFC1918 address reachable from the node's environment).
2. Ensure the malicious peer also advertises `STACKERDB` service flags and/or waits for the target's periodic mempool sync.
3. Observe (e.g., via a listener on the target internal address, or via response-timing/behavior differences) that the target node originates outbound HTTP connections to the attacker-chosen internal address as part of its StackerDB replica sync (`StackerDBSync::connect_begin`) or mempool sync (`mempool_sync_send_query`), demonstrating SSRF against the node's own internal network surface.

### Citations

**File:** stackslib/src/net/chat.rs (L1131-1148)
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

**File:** stackslib/src/net/chat.rs (L2780-2914)
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

**File:** stackslib/src/util_lib/strings.rs (L110-151)
```rust
impl UrlString {
    /// Determine that the UrlString parses to something that can be used to fetch blocks via HTTP(S).
    /// A block URL must be an HTTP(S) URL without a query or fragment, and without a login.
    pub fn parse_to_block_url(&self) -> Result<url::Url, codec_error> {
        // even though this code uses from_utf8_unchecked() internally, we've already verified that
        // the bytes in this string are all ASCII.
        let url = url::Url::parse(&self.to_string())
            .map_err(|e| codec_error::DeserializeError(format!("Invalid URL: {:?}", &e)))?;

        if url.scheme() != "http" && url.scheme() != "https" {
            return Err(codec_error::DeserializeError(format!(
                "Invalid URL: invalid scheme '{}'",
                url.scheme()
            )));
        }

        if !url.username().is_empty() || url.password().is_some() {
            return Err(codec_error::DeserializeError(
                "Invalid URL: must not contain a username/password".to_string(),
            ));
        }

        if url.host_str().is_none() {
            return Err(codec_error::DeserializeError(
                "Invalid URL: no host string".to_string(),
            ));
        }

        if url.query().is_some() {
            return Err(codec_error::DeserializeError(
                "Invalid URL: query strings not supported for block URLs".to_string(),
            ));
        }

        if url.fragment().is_some() {
            return Err(codec_error::DeserializeError(
                "Invalid URL: fragments are not supported for block URLs".to_string(),
            ));
        }

        Ok(url)
    }
```

**File:** stackslib/src/net/neighbors/rpc.rs (L191-249)
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

        debug!(
            "Send request to {} on event {}: {:?}",
            &naddr, event_id, &request
        );
        self.state.insert(naddr, (event_id, Some(request)));
        Ok(())
    }
```

**File:** stackslib/src/net/httpcore.rs (L1873-1911)
```rust
impl PeerNetwork {
    /// Send a (non-blocking) HTTP request to a remote peer.
    /// Returns the event ID on success.
    #[cfg_attr(test, mutants::skip)]
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

**File:** stackslib/src/net/mempool/mod.rs (L317-342)
```rust
    /// Ask the remote peer for its mempool, connecting to it in the process if need be.
    /// Returns Ok((true, ..)) if we're done mempool syncing
    /// Returns Ok((false, ..)) if there's more to do
    /// Returns the event ID on success
    #[cfg_attr(test, mutants::skip)]
    fn mempool_sync_send_query(
        &mut self,
        network: &mut PeerNetwork,
        url: &UrlString,
        addr: &SocketAddr,
        mempool: &MemPoolDB,
        page_id: Txid,
    ) -> Result<(bool, Option<usize>), NetError> {
        let sync_data = mempool.make_mempool_sync_data()?;
        let request = StacksHttpRequest::new_for_peer(
            PeerHost::from_socketaddr(addr),
            "POST".into(),
            self.api_endpoint.clone(),
            HttpRequestContents::new()
                .query_arg("page_id".into(), format!("{}", &page_id))
                .payload_stacks(&sync_data),
        )?;

        let event_id = network.connect_or_send_http_request(url.clone(), *addr, request)?;
        return Ok((false, Some(event_id)));
    }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L713-797)
```rust
    /// Establish sessions with remote replicas.
    /// We might not be connected to any yet.
    /// Clears self.replicas, and fills in self.connected_replicas with already-connected neighbors
    /// Returns Ok(true) if we can proceed to sync
    /// Returns Ok(false) if we should try this again
    /// Returns Err(NoSuchNeighbor) if we don't have anyone to talk to
    /// Returns Err(..) on DB query error
    pub fn connect_begin(&mut self, network: &mut PeerNetwork) -> Result<bool, net_error> {
        if self.replicas.is_empty() {
            // find some from the peer DB
            let replicas = self.find_qualified_replicas(network)?;
            self.replicas = replicas;
        }
        debug!(
            "{:?}: {}: connect_begin: establish StackerDB sessions to {} neighbors (out of {} p2p peers)",
            network.get_local_peer(),
            &self.smart_contract_id,
            self.replicas.len(),
            network.get_num_p2p_convos();
            "replicas" => ?self.replicas
        );
        if self.replicas.is_empty() {
            // nothing to do
            return Err(net_error::NoSuchNeighbor);
        }

        let naddrs = mem::replace(&mut self.replicas, HashSet::new());
        for naddr in naddrs.into_iter() {
            if self.comms.is_neighbor_connecting(network, &naddr) {
                debug!(
                    "{:?}: {}: connect_begin: already connecting to StackerDB peer {:?}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    &naddr
                );
                self.replicas.insert(naddr);
                continue;
            }
            if self.comms.has_neighbor_session(network, &naddr) {
                debug!(
                    "{:?}: {}: connect_begin: already connected to StackerDB peer {:?}",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    &naddr
                );
                self.connected_replicas.insert(naddr);
                continue;
            }

            debug!(
                "{:?}: {}: connect_begin: Send Handshake to StackerDB peer {:?}",
                network.get_local_peer(),
                &self.smart_contract_id,
                &naddr
            );
            match self.comms.neighbor_session_begin(network, &naddr) {
                Ok(true) => {
                    // connected!
                    debug!(
                        "{:?}: {}: connect_begin: connected to StackerDB peer {:?}",
                        network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr
                    );
                    self.num_attempted_connections += 1;
                    self.num_connections += 1;
                    self.connected_replicas.insert(naddr);
                }
                Ok(false) => {
                    // need to retry
                    self.num_attempted_connections += 1;
                    self.replicas.insert(naddr);
                }
                Err(_e) => {
                    debug!(
                        "{:?}: {}: Failed to begin session with {:?}: {:?}",
                        &network.get_local_peer(),
                        &self.smart_contract_id,
                        &naddr,
                        &_e
                    );
                }
            }
        }
        Ok(!self.connected_replicas.is_empty())
```
