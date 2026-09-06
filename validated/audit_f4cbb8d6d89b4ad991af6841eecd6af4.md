### Title
SSRF-style forced outbound connection to attacker-chosen (including private/internal) addresses via unchecked peer-supplied `data_url` in Nakamoto tenure download and Atlas attachment sync - (File: stackslib/src/net/neighbors/rpc.rs)

### Summary
A remote, unauthenticated-at-the-application-layer Stacks peer supplies an arbitrary `data_url` in its `Handshake`/`HandshakeAccept` message [1](#0-0) . This URL is resolved to an IP address and cached on the conversation as `data_ip` [2](#0-1) , without ever being checked against `is_in_private_range()`/`private_neighbors` the way other consumers of peer-controlled addresses are (e.g. mempool sync explicitly does this check before connecting) [3](#0-2) . Both `NeighborRPC::send_request` (used by the Nakamoto tenure downloader) and the Atlas attachment downloader take this unchecked `data_url`/resolved IP and open a fresh outbound HTTP connection to it.

### Finding Description
`validate_handshake` only requires that the *socket-observed* peer address match `handshake_data.addrbytes`/`port` for outbound connections; it never validates or restricts `handshake_data.data_url`, which is a free-form string field decoded straight off the wire [4](#0-3) [5](#0-4) . This `data_url` is stored on the `ConversationP2P` as `self.data_url` and resolved via DNS (or parsed directly if a bare IP) into `self.data_ip` with no private/loopback/link-local filtering [6](#0-5) [7](#0-6) .

Two downstream consumers then use this attacker-controlled `data_url`/resolved address to *initiate a brand-new outbound TCP+HTTP connection*, distinct from the already-authenticated p2p socket:

1. `NeighborRPC::get_peer_host`/`send_request` (used by the Nakamoto tenure downloader, `stackslib/src/net/download/nakamoto/tenure_downloader.rs`) takes `convo.data_url` and `convo.data_ip` and calls `http.connect_http(...)` to open a new connection to that address [8](#0-7) . No `is_in_private_range` or `private_neighbors` check exists in this file (confirmed absent by search).
2. The Atlas attachment downloader collects `peer_url` values via `network.get_data_url(&peer)` (again sourced from `convo.data_url`) and issues `AttachmentsInventoryRequest`/`AttachmentRequest` HTTP GETs to that URL/host, again with no private-range filtering in `stackslib/src/net/atlas/download.rs` (confirmed absent by search) [9](#0-8) [10](#0-9) .

This breaks the equality the codebase otherwise enforces elsewhere: "an address supplied by an untrusted remote peer must be checked against `private_neighbors`/`is_in_private_range` before the node uses it to originate a connection." That invariant is enforced for neighbor-walk targets (`filter_sensible_neighbors`) [11](#0-10) , for StackerDB hint-replicas from a smart contract (`eval_hint_replicas` skips private IPs) [12](#0-11) , and for mempool sync (`do_mempool_sync`'s explicit `is_in_private_range` gate) [3](#0-2) , but is missing for the tenure-download and Atlas-attachment HTTP paths that key off the peer-supplied `data_url` directly.

### Impact Explanation
Any remote peer that can complete a p2p handshake (unauthenticated, unprivileged — handshakes are accepted from any connecting IP unless `disable_inbound_handshakes` is set) can set its `data_url` to point at an internal/private address (e.g. `127.0.0.1`, RFC1918 ranges, a cloud metadata endpoint, or another internal service) reachable from the victim node's network position. The victim node will then originate genuine outbound TCP/HTTP connections to that address as part of routine tenure-download and Atlas-sync activity, repeatedly and automatically (these are scheduled background sync loops, not one-off requests). This is a classic SSRF primitive: it lets an attacker use the Stacks node as a network proxy to probe/interact with internal services that would otherwise be unreachable from the attacker's vantage point, and can be used for internal network reconnaissance or to pivot into internal-only HTTP services. It does not, by itself, leak response bodies back to the attacker (the node just tries to parse the response as tenure/attachment protocol data and will typically fail/discard it), which caps this below "memory disclosure," but it squarely matches the "unauthorized outbound request to internal resources" bug class from the report, mapped onto this repo's HTTP client paths.

### Likelihood Explanation
Likelihood is moderate-to-high: initiating a handshake with a crafted `data_url` requires no authentication, no valid keys beyond a normal handshake signature, and no special privileges — it is standard p2p behavior. The victim's tenure-downloader and Atlas-sync state machines run continuously in the background and will pick up any peer advertising a plausible `data_url`, making exploitation largely automatic once the malicious peer is connected as a neighbor.

### Recommendation
Apply the same private/loopback/link-local address filtering used in `do_mempool_sync` (`PeerAddress::from_socketaddr(addr).is_in_private_range()` gated by `connection_opts.private_neighbors`) to:
- `NeighborRPC::get_peer_host`/`send_request` in `stackslib/src/net/neighbors/rpc.rs`, before calling `http.connect_http`, and
- the Atlas download machinery in `stackslib/src/net/atlas/download.rs`, before dispatching `AttachmentsInventoryRequest`/`AttachmentRequest` to a peer's `data_url`.

Additionally, consider validating `HandshakeData.data_url`'s resolved address at handshake-acceptance time (in `try_resolve_data_url_host`/`chat.rs`) so that a private/loopback resolution is rejected or ignored unless `private_neighbors` is explicitly enabled, consistent with how private IPs are already handled for the peer's primary `addrbytes`.

### Proof of Concept
1. Stand up a malicious Stacks node that completes a normal p2p handshake with the target node, but sets `HandshakeData.data_url` to `http://127.0.0.1:<internal-port>/` (or an internal-only address reachable from the target, e.g. `http://169.254.169.254/`).
2. Wait for the target's Nakamoto tenure-downloader (or Atlas attachment downloader) to select this peer as a download source — this happens automatically since the malicious peer is a normal, connected neighbor.
3. Observe (e.g., via a listener at the target address, or via connection logs/timing on the internal endpoint) that the target node originates an outbound HTTP connection to the attacker-chosen internal address, driven by `NeighborRPC::send_request`/`connect_http` or the Atlas `AttachmentsInventoryRequest`/`AttachmentRequest` machinery, with no private-range check preventing it.

**Note on verification limits:** I was not able to fully trace every call site of `http.connect_http` used for Atlas downloads (i.e., where `AttachmentsInventoryRequest`/`AttachmentRequest`'s `Requestable::get_url()` is turned into an actual socket connection) within the remaining tool budget; I confirmed the absence of `is_in_private_range`/`private_neighbors` checks in `atlas/download.rs` and `neighbors/rpc.rs` via direct grep, but recommend a Devin session with fuller repo access to confirm the exact connection-establishment call sites end-to-end before treating this as fully proven.

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

**File:** stackslib/src/net/chat.rs (L1072-1091)
```rust
            Some(_) => {
                // for outbound connections, the self-reported address must match socket address if we already have a public key.
                // (not the case for inbound connections, since the peer socket address we see may
                // not be the same as the address the remote peer thinks it has).
                // The only exception to this is if the remote peer does not yet know its own
                // public IP address, in which case, its handshake addrbytes will be the
                // any-network bind address (0.0.0.0 or ::)
                if self.stats.outbound
                    && (!handshake_data.addrbytes.is_anynet()
                        && (self.peer_addrbytes != handshake_data.addrbytes
                            || self.peer_port != handshake_data.port))
                {
                    // wrong peer address
                    debug!(
                        "{:?}: invalid handshake -- wrong addr/port ({:?}:{:?})",
                        &self, &handshake_data.addrbytes, handshake_data.port
                    );
                    return Err(net_error::InvalidHandshake);
                }
            }
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

**File:** stackslib/src/net/chat.rs (L2800-2818)
```rust
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
```

**File:** stackslib/src/net/chat.rs (L2867-2910)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L1028-1055)
```rust
impl Requestable for AttachmentsInventoryRequest {
    fn get_url(&self) -> &UrlString {
        &self.url
    }

    fn make_request_type(&self, peer_host: PeerHost) -> StacksHttpRequest {
        let mut page_indexes = HashSet::new();
        for page in self.pages.iter() {
            page_indexes.insert(*page);
        }
        let mut page_list: Vec<String> = page_indexes
            .into_iter()
            .map(|i| format!("{}", &i))
            .collect();
        page_list.sort();
        StacksHttpRequest::new_for_peer(
            peer_host,
            "GET".into(),
            "/v2/attachments/inv".into(),
            HttpRequestContents::new()
                .query_arg(
                    "index_block_hash".into(),
                    format!("{}", &self.index_block_hash),
                )
                .query_arg("pages_indexes".into(), page_list[..].join(",")),
        )
        .expect("FATAL: failed to create an HTTP request for infallible data")
    }
```

**File:** stackslib/src/net/atlas/download.rs (L1104-1119)
```rust
impl Requestable for AttachmentRequest {
    fn get_url(&self) -> &UrlString {
        let (url, _) = self.get_most_reliable_source();
        url
    }

    fn make_request_type(&self, peer_host: PeerHost) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            peer_host,
            "GET".to_string(),
            format!("/v2/attachments/{}", &self.content_hash),
            HttpRequestContents::new(),
        )
        .expect("FATAL: failed to create an HTTP request for infallible data")
    }
}
```

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
