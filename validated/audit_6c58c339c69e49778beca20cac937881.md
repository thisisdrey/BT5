I have enough evidence to write the finding now.

### Title
SSRF via unvalidated peer-supplied `data_url` in block/attachment/StackerDB HTTP fetch paths — ([File: stackslib/src/net/download/epoch2x.rs])

### Summary
A remote, unauthenticated peer can advertise an arbitrary `data_url` (including one that resolves to `127.0.0.1`, RFC1918/loopback/link-local addresses, or any internal service) in its `Handshake`/`HandshakeAccept` message. The node's block downloader (`epoch2x.rs`), Atlas attachment downloader, and StackerDB neighbor-RPC client all resolve and connect to this URL over HTTP without ever checking whether the resolved IP is private/local — unlike the mempool-sync code path, which explicitly performs this check. This lets an attacker coerce the victim node into making outbound HTTP requests to arbitrary internal/loopback endpoints on the node's own host or LAN — a classic SSRF, directly analogous to the reported `nossrf` bug class (hostname resolves to a local/reserved address, bypassing intended protection).

### Finding Description
The `data_url` field is attacker-controlled: it is set from `HandshakeData.data_url` (a `UrlString`, deserialized with no host restriction, [1](#0-0) ), and stored verbatim on the conversation via `update_from_handshake_data` (`self.data_url = handshake_data.data_url.clone();`) [2](#0-1) .

Later, multiple subsystems resolve this `data_url` to a `SocketAddr` and open outbound HTTP connections to it:
- Block/microblock downloader: `dns_lookups_begin` resolves the URL's host/IP with no private-range filtering [3](#0-2) , and `begin_request` connects directly to the resolved `SocketAddr` via `connect_or_send_http_request` [4](#0-3) .
- Atlas attachment downloader: `BatchedDNSLookupsState::try_proceed` performs the identical unfiltered resolution [5](#0-4) .
- StackerDB neighbor RPC (`NeighborRPC::send_request`) uses the authenticated conversation's `data_ip`/`data_url` directly with `http.connect_http(...)`, with no IP-range check [6](#0-5) .
- `PeerNetwork::try_get_url_ip` even hard-codes `localhost` → `127.0.0.1` as an acceptable resolution target [7](#0-6) .
- The actual TCP connect and HTTP send happen in `connect_http`/`send_http_request`, which take the caller-provided address with no validation of its origin [8](#0-7) [9](#0-8) .

Contrast this with mempool sync, which does check the resolved address before issuing a request:
```
if !network.get_connection_opts().private_neighbors
    && PeerAddress::from_socketaddr(addr).is_in_private_range()
{
    ... skip sync, don't connect ...
}
``` [10](#0-9) 

The existence of `PeerAddress::is_in_private_range()` [11](#0-10)  and the `private_neighbors` config flag, whose documentation states the node should "Avoid initiating connections to peers known to have private IPs" and "Skip querying peers with private IPs for mempool or StackerDB data" [12](#0-11) , confirms this is an intentional protection that mempool sync honors but the block downloader, Atlas downloader, and StackerDB neighbor-RPC paths do not.

### Impact Explanation
By advertising a `data_url` such as `http://127.0.0.1:<internal-port>/...` or `http://169.254.169.254/...` during handshake, a remote unauthenticated peer can cause the victim node to issue outbound Stacks-formatted HTTP GET/POST requests to arbitrary addresses reachable from the node's network namespace — including services bound only to loopback (e.g. an unauthenticated local RPC/monitoring endpoint, or another local Stacks node's admin surface) or internal/cloud-metadata IPs. This is a bounded-compute, unauthenticated read-endpoint style SSRF reachable from a single remote peer, matching the "High" impact tier (steering requests to unintended internal endpoints via forged advertised data).

### Likelihood Explanation
Likelihood is high: any peer that completes a handshake (which requires no authorization beyond a valid signature over attacker-controlled keys) can set an arbitrary `data_url`. The block/microblock downloader and Atlas downloader routinely and automatically resolve and connect to peer `data_url`s during normal chain sync, so no additional user interaction is required.

### Recommendation
Apply the same check used in mempool sync (`PeerAddress::from_socketaddr(addr).is_in_private_range()` gated by `connection_opts.private_neighbors`) uniformly to every outbound HTTP connection driven by a peer-supplied `data_url`: in `BlockDownloader::dns_lookups_begin`/`begin_request` (`stackslib/src/net/download/epoch2x.rs`), in the Atlas `BatchedDNSLookupsState` resolution path (`stackslib/src/net/atlas/download.rs`), and in `NeighborRPC::send_request` / `PeerNetwork::connect_or_send_http_request` (`stackslib/src/net/neighbors/rpc.rs`, `stackslib/src/net/httpcore.rs`). Centralizing the check inside `HttpPeer::connect_http` (`stackslib/src/net/server.rs`) or `PeerNetwork::connect_or_send_http_request` would ensure all call sites get the protection consistently rather than relying on each caller to remember to check.

### Proof of Concept
1. Stand up a malicious peer that completes the Stacks P2P handshake with the victim node, setting `HandshakeData.data_url` to `http://127.0.0.1:<port-of-a-sensitive-local-service>/`.
2. Wait for the victim's block/microblock or Atlas-attachment downloader to select this peer as a source for a needed block/microblock/attachment (or have the StackerDB neighbor-RPC client issue a request to it).
3. Observe that `dns_lookups_begin`/`try_proceed` resolves the loopback IP unfiltered, and `begin_request`/`connect_or_send_http_request`/`connect_http` establish a TCP connection and send a crafted Stacks HTTP request to `127.0.0.1:<port>`, whereas the same scenario driven through mempool sync (`mempool_sync_pick_outbound_peer` → `SendQuery` state) is correctly rejected by the `is_in_private_range()` guard in `stackslib/src/net/mempool/mod.rs:488-498`.

### Citations

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

**File:** stackslib/src/net/chat.rs (L1129-1147)
```rust
    /// Update connection state from handshake data.
    /// Returns true if we learned a new public key; false if not
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

**File:** stackslib/src/net/download/epoch2x.rs (L308-357)
```rust
    pub fn dns_lookups_begin(
        &mut self,
        pox_id: &PoxId,
        dns_client: &mut DNSClient,
        urls: Vec<UrlString>,
    ) -> Result<(), net_error> {
        assert_eq!(self.state, BlockDownloaderState::DNSLookupBegin);

        // optimistic concurrency control: remember the current PoX Id
        self.pox_id = pox_id.clone();
        self.dns_lookups.clear();
        for url_str in urls.into_iter() {
            if url_str.is_empty() {
                continue;
            }
            let url = url_str.parse_to_block_url()?; // NOTE: should always succeed, since a UrlString shouldn't decode unless it's a valid URL or the empty string
            let port = match url.port_or_known_default() {
                Some(p) => p,
                None => {
                    warn!("Unsupported URL {:?}: unknown port", &url);
                    continue;
                }
            };
            match url.host() {
                Some(url::Host::Domain(domain)) => {
                    match dns_client.queue_lookup(
                        domain,
                        port,
                        get_epoch_time_ms() + self.dns_timeout,
                    ) {
                        Ok(_) => {}
                        Err(_) => continue,
                    }
                    self.dns_lookups.insert(url_str.clone(), None);
                    self.parsed_urls
                        .insert(url_str, DNSRequest::new(domain.to_string(), port, 0));
                }
                Some(url::Host::Ipv4(addr)) => {
                    self.dns_lookups
                        .insert(url_str, Some(vec![SocketAddr::new(IpAddr::V4(addr), port)]));
                }
                Some(url::Host::Ipv6(addr)) => {
                    self.dns_lookups
                        .insert(url_str, Some(vec![SocketAddr::new(IpAddr::V6(addr), port)]));
                }
                None => {
                    warn!("Unsupported URL {:?}", &url_str);
                }
            }
        }
```

**File:** stackslib/src/net/download/epoch2x.rs (L1916-1957)
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

**File:** stackslib/src/net/atlas/download.rs (L677-742)
```rust
    fn try_proceed(
        fsm: BatchedDNSLookupsState,
        dns_client: &mut DNSClient,
        connection_options: &ConnectionOptions,
    ) -> BatchedDNSLookupsState {
        let mut fsm = fsm;
        match fsm {
            BatchedDNSLookupsState::Initialized(ref mut urls) => {
                let mut state = BatchedDNSLookupsResults::default();

                for url_str in urls.drain(..) {
                    if url_str.is_empty() {
                        continue;
                    }
                    let url = match url_str.parse_to_block_url() {
                        Ok(url) => url,
                        Err(e) => {
                            warn!("Atlas: Unsupported URL {:?}, {}", url_str, e);
                            state.errors.insert(url_str, e.into());
                            continue;
                        }
                    };
                    let port = match url.port_or_known_default() {
                        Some(p) => p,
                        None => {
                            warn!("Atlas: Unsupported URL {:?}: unknown port", &url);
                            continue;
                        }
                    };
                    match url.host() {
                        Some(url::Host::Domain(domain)) => {
                            let res = dns_client.queue_lookup(
                                domain,
                                port,
                                get_epoch_time_ms() + connection_options.dns_timeout,
                            );
                            match res {
                                Ok(_) => {
                                    state.dns_lookups.insert(url_str.clone(), None);
                                    state.parsed_urls.insert(
                                        url_str,
                                        DNSRequest::new(domain.to_string(), port, 0),
                                    );
                                }
                                Err(e) => {
                                    state.errors.insert(url_str.clone(), e);
                                }
                            }
                        }
                        Some(url::Host::Ipv4(addr)) => {
                            state.dns_lookups.insert(
                                url_str,
                                Some(vec![SocketAddr::new(IpAddr::V4(addr), port)]),
                            );
                        }
                        Some(url::Host::Ipv6(addr)) => {
                            state.dns_lookups.insert(
                                url_str,
                                Some(vec![SocketAddr::new(IpAddr::V6(addr), port)]),
                            );
                        }
                        None => {
                            warn!("Atlas: Unsupported URL {:?}", &url_str);
                        }
                    }
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

**File:** stackslib/src/net/p2p.rs (L3854-3882)
```rust
    /// Extract an IP address from a UrlString if it exists
    pub fn try_get_url_ip(url_str: &UrlString) -> Result<Option<SocketAddr>, net_error> {
        let url = url_str.parse_to_block_url()?;
        let port = match url.port_or_known_default() {
            Some(p) => p,
            None => {
                warn!("Unsupported URL {:?}: unknown port", &url);
                return Ok(None);
            }
        };
        match url.host() {
            Some(url::Host::Domain(d)) => {
                if d == "localhost" {
                    Ok(Some(SocketAddr::new(
                        IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)),
                        port,
                    )))
                } else {
                    // can't use this
                    Ok(None)
                }
            }
            Some(url::Host::Ipv4(addr)) => Ok(Some(SocketAddr::new(IpAddr::V4(addr), port))),
            Some(url::Host::Ipv6(addr)) => Ok(Some(SocketAddr::new(IpAddr::V6(addr), port))),
            None => {
                warn!("Unsupported URL {:?}", &url_str);
                Ok(None)
            }
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

**File:** stackslib/src/net/httpcore.rs (L1942-1954)
```rust
/// Send an HTTP request to the given host:port.  Returns the decoded response.
/// Internally, this creates a socket, connects it, sends the HTTP request, and decodes the HTTP
/// response.  It is a blocking operation.
///
/// If the request encounters a network error, then return an error.  Don't retry.
/// If the request times out after `timeout`, then return an error.
pub fn send_http_request(
    host: &str,
    port: u16,
    request: StacksHttpRequest,
    timeout: Duration,
) -> Result<StacksHttpResponse, io::Error> {
    // Find the host:port that works.
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

**File:** stackslib/src/config/mod.rs (L3788-3800)
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
```
