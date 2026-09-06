### Title
SSRF-analog: attacker-controlled `data_url` in Handshake causes outbound HTTP requests to arbitrary/internal addresses - (File: stackslib/src/net/chat.rs, stackslib/src/net/dns.rs)

### Summary
An unprivileged remote peer supplies an arbitrary `data_url` string in the `HandshakeData` it sends during a normal P2P handshake. This URL is stored verbatim (`ConversationHttp::data_url` / `NeighborKey`/`convo.data_url`) and later used by several node-driven subsystems (block/microblock/mempool sync, and `PeerNetwork::connect_or_send_http_request`) to open outbound TCP/HTTP connections. Unlike the gossip-derived `NeighborAddress` list, which is explicitly filtered against private/link-local ranges before being dialed (`filter_sensible_neighbors` at [1](#0-0) ), the `data_url` field is never checked against private/link-local/loopback ranges anywhere in the DNS-resolution or connect path.

### Finding Description
The `data_url` field originates entirely from the remote, unauthenticated-at-handshake-time peer: [2](#0-1) 

It is copied straight from the handshake into the local conversation state with no host/IP validation: [3](#0-2) 

When the node later needs to resolve and connect to this URL (e.g., for HTTP RPC/mempool/block-download requests), IP-literal addresses are used as-is and domain names are looked up via a plain OS `to_socket_addrs()` call with no filtering of loopback/private/link-local results: [4](#0-3) [5](#0-4) 

The resolved address is then dialed directly by `NetworkState::connect`/`TcpStream::connect_timeout` with no `is_in_private_range()`/anynet check, unlike the analogous gossip-neighbor path: [6](#0-5) [7](#0-6) 

Compare this to the neighbor-gossip path, which *does* filter private/anynet destinations before ever attempting a connection: [1](#0-0) 

This is the same bug class as the HackerOne report: a service accepts a caller-supplied endpoint/host and issues an outbound network request to it without validating that the destination isn't an internal/private/link-local address.

### Impact Explanation
Any node participating in the P2P network (no special privilege required beyond completing a handshake, which is unauthenticated by design) can set its advertised `data_url` to a target of its choosing (`http://127.0.0.1:<internal-port>/...`, `http://169.254.169.254/`, `http://<internal-service>/...`, or any RFC1918 address reachable from the victim). Whenever the victim node subsequently attempts to reach that peer for block/microblock/mempool/HTTP RPC data (`mempool_sync_send_query`, `PeerNetwork::begin_request`, `connect_or_send_http_request`), it will issue an outbound HTTP request built with a Stacks-specific request line/headers to the attacker-chosen internal target. This can be used to:
- probe/port-scan a victim's internal network from the perspective of the Stacks node,
- trigger unintended requests against internal-only HTTP services (cloud metadata endpoints, admin panels, etc.) reachable only from the node's network position,
- potentially cause side effects on internal services that act on unauthenticated HTTP requests.

This lines up with the "unauthorized write to state" / "bounded compute DoS on a read endpoint" spectrum only loosely — the most defensible framing is High-severity SSRF: the node can be steered into issuing requests to internal infrastructure it would not otherwise contact, with no confirmation that the destination is actually the honest, gossip-validated peer.

### Likelihood Explanation
Likelihood is high for reaching the vulnerable code path: any peer that completes a handshake (an unauthenticated, low-cost interaction) can supply an arbitrary `data_url`, and legitimate node logic (mempool sync, block/microblock download, ad hoc HTTP RPC) will automatically attempt to dial it during normal operation without further consent from the operator. No secret keys, admin roles, or elevated privileges are needed — only the ability to open a P2P connection to the victim node.

### Recommendation
Before dialing a peer-supplied `data_url` (in `try_resolve_data_url_host`/`try_decode_data_url_ipaddr` in `stackslib/src/net/chat.rs`, and in `DNSResolver::resolve` in `stackslib/src/net/dns.rs`), apply the same routability checks already used for `NeighborAddress` gossip (`PeerAddress::is_in_private_range()`, `is_anynet()`), rejecting or gating (behind the existing `private_neighbors` config flag) any resolved address that falls in loopback/link-local/private ranges, consistent with `NeighborWalk::filter_sensible_neighbors`.

### Proof of Concept
1. Stand up a malicious peer that completes the standard P2P handshake with a victim node, sending a `Handshake` message whose `HandshakeData.data_url` is set to `http://169.254.169.254/latest/meta-data/` (or `http://127.0.0.1:<internal-port>/`).
2. The victim's `ConversationHttp::update_from_handshake_data` stores this URL unchanged (`stackslib/src/net/chat.rs:1147`).
3. Trigger any subsequent data-fetch cycle that uses this peer as a source — e.g., wait for the victim's periodic mempool sync (`mempool_sync_send_query`) or a block/microblock download round that selects this peer via `PeerNetwork::begin_request`.
4. Observe (via network capture on the victim, or via timing/behavioral side effects on the internal target) that the victim node issues an outbound HTTP request to the attacker-chosen internal address, confirming the SSRF-analog.

*Note:* I was not able to fully trace whether any HTTP response bytes fetched from the forged `data_url` are ever reflected back to the originating attacker peer (which would upgrade this from "internal request forgery" to full response-disclosure SSRF); the index does not show such a reflection path in the resources I inspected (`mempool/mod.rs`, `download/epoch2x.rs`, `httpcore.rs`). If a full audit is needed to confirm/rule out response leakage, that would require a broader trace than what the current search results cover.

### Citations

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

**File:** stackslib/src/net/chat.rs (L1129-1148)
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

**File:** stackslib/src/net/chat.rs (L2780-2798)
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
```

**File:** stackslib/src/net/dns.rs (L125-151)
```rust
    pub fn resolve(&self, req: DNSRequest) -> DNSResponse {
        if let Some(addrs) = self.hardcoded.get(&(req.host.clone(), req.port)) {
            return DNSResponse::new(req, Ok(addrs.to_vec()));
        }

        // TODO: this is a blocking operation, but there's not really a good solution here other
        // than to just do this in a separate thread :shrug:
        test_debug!("Resolve {}:{}", &req.host, req.port);
        let addrs: Vec<SocketAddr> = match (req.host.as_str(), req.port).to_socket_addrs() {
            Ok(iter) => {
                let mut list = vec![];
                for addr in iter {
                    list.push(addr);
                }
                list
            }
            Err(ioe) => {
                return DNSResponse::error(req, format!("DNS resolve error: {:?}", &ioe));
            }
        };

        if addrs.is_empty() {
            return DNSResponse::error(req, "DNS resolve error: got zero addresses".to_string());
        }
        test_debug!("{}:{} resolved to {:?}", &req.host, req.port, &addrs);
        DNSResponse::new(req, Ok(addrs))
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

**File:** stackslib/src/net/httpcore.rs (L1942-1979)
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
    // This is sometimes necessary because `localhost` can resolve to both its ipv4 and ipv6
    // addresses, but usually, Stacks services like event observers are only bound to ipv4
    // addresses.  So, be sure to use an address that will lead to a socket connection!
    let mut stream_and_addr = None;
    let mut last_err = None;
    for addr in format!("{host}:{port}").to_socket_addrs()? {
        debug!("send_request: connect to {}", &addr);
        match TcpStream::connect_timeout(&addr, timeout) {
            Ok(sock) => {
                stream_and_addr = Some((sock, addr));
                break;
            }
            Err(e) => {
                last_err = Some(e);
            }
        }
    }

    let Some((mut stream, addr)) = stream_and_addr else {
        return Err(last_err.unwrap_or(io::Error::other("Unable to connect to {host}:{port}")));
    };

    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    stream.set_nodelay(true)?;
```
