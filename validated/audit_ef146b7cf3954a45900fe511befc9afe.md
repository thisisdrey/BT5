### Title
Unvalidated `data_url` from handshake steers node-initiated Atlas attachment fetches to attacker-chosen hosts - (File: stackslib/src/net/chat.rs)

### Summary
`ConversationP2P::update_from_handshake_data` copies `handshake_data.data_url` into `self.data_url` with no cross-check against `handshake_data.addrbytes`/`port`, and this URL is persisted to `PeerDB` via `Neighbor::handshake_update`/`save_update`. The Atlas attachment downloader (`AttachmentsDownloader`) later uses `PeerNetwork::get_data_url` (which returns this attacker-supplied URL verbatim) to actually perform DNS resolution and HTTP GET requests for `/v2/attachments/*`, meaning any peer that handshakes with a node can redirect that node's outbound attachment fetches to an arbitrary host/port of the attacker's choosing.

### Finding Description
In `update_from_handshake_data` at `stackslib/src/net/chat.rs:1147`, `self.data_url = handshake_data.data_url.clone();` is set directly from the remote-supplied `HandshakeData`, with no check that the URL's host resolves to `handshake_data.addrbytes`/`port` or to the actual TCP peer address of the connection. [1](#0-0) 

This same unvalidated value is persisted into `PeerDB` through `Neighbor::handshake_update`, which is invoked from `handle_handshake`/`handle_handshake_accept` paths and stored via `neighbor.save_update`. [2](#0-1) [3](#0-2) 

Downstream, `PeerNetwork::get_data_url` returns `convo.data_url` unmodified for use by the Atlas downloader: [4](#0-3) 

The `AttachmentsDownloader::run` gathers these peer URLs from all outbound sync peers and feeds them into `AttachmentsBatchStateContext`, which drives DNS resolution (`BatchedDNSLookupsState`) and issues real HTTP requests (`AttachmentsInventoryRequest::make_request_type`, `AttachmentRequest::make_request_type`) to fetch `/v2/attachments/inv` and `/v2/attachments/{hash}`. [5](#0-4) [6](#0-5) [7](#0-6) 

There is no validation anywhere in this path that the resolved host/port of `data_url` matches `handshake_data.addrbytes`/`port`. The only defensive logic present is `try_decode_data_url_ipaddr`/`try_resolve_data_url_host` (used for a different purpose — resolving the peer's own advertised IP for outbound inbound-neighbor bookkeeping), which likewise performs no comparison against `addrbytes`. [8](#0-7) 

So the broken equality is real: `data_url` served to the Atlas downloader is **not** guaranteed to equal a URL reachable at/controlled by the claimed `addrbytes:port` identity — it's simply whatever string the remote peer put in its handshake. An attacker who performs a normal handshake with a victim node (an unprivileged, remote action any peer can take) can set `data_url` to `http://internal-service.victim-lan:8080/` or to a third party's RPC endpoint, and the victim node will resolve and connect to that host when trying to sync Atlas attachments from "this peer," constituting an SSRF-style redirection of node-initiated HTTP traffic.

### Impact Explanation
This causes the victim node to make outbound HTTP requests (DNS resolution + TCP connect + HTTP GET) to a host chosen entirely by the attacker's handshake payload, disguised as if that host is a peer's legitimate `/v2/attachments` endpoint. This matches the "attachment/BNS mismatch" High-severity category: the node ends up treating attacker-chosen URLs as canonical sources for attachment inventories/downloads. It does not corrupt AtlasDB state directly (responses are still validated against content hashes before being trusted as an `Attachment` — see `check_attachment_instances`), but it is a repeatable SSRF primitive against the node's outbound HTTP client, usable to probe internal network services or to trigger a request storm against arbitrary internet hosts under the victim node's identity.

### Likelihood Explanation
Any remote party that can establish a normal P2P handshake (no secret, no privileged role, no StackerDB ownership required) can set this field; this happens on every handshake, including inbound-initiated ones, and is a single crafted message. It's fully repeatable — the attacker can re-handshake and rotate the target URL at will, and is exploitable by any outbound-sync peer entry that the node maintains (the requirement is simply that the node treats the sender as an "outbound sync peer" candidate for Atlas, which follows from ordinary peer walk/frontier logic).

### Recommendation
Before persisting or using `data_url` for outbound fetches, validate that its host (after DNS resolution, for domain names) matches `handshake_data.addrbytes`, or restrict `data_url` acceptance to only the same IP the TCP connection was established with (as is loosely intended by `try_decode_data_url_ipaddr`/`try_resolve_data_url_host`, but those functions currently only compute the IP without ever comparing it to `addrbytes`). Alternatively, disallow arbitrary data URLs entirely and always derive the fetch target from `addrbytes:port` plus a fixed RPC path scheme, only allowing the advertised URL to override the port, not the host.

### Proof of Concept
Rust test plan in `stackslib::net::chat`:
1. Construct two `ConversationP2P` instances as in existing tests (e.g. `convo_1`/`convo_2` pattern used at `stackslib/src/net/chat.rs:3485`).
2. Craft `HandshakeData` for peer 1 with `addrbytes`/`port` set to peer 1's real bind address, but `data_url = UrlString::from_literal("http://169.254.169.254:80/")` (or any attacker-controlled/internal host unrelated to `addrbytes`).
3. Send the handshake from convo_1 to convo_2 and drive `convo_2.chat(...)`.
4. Assert `convo_2.data_url == UrlString::from_literal("http://169.254.169.254:80/")` (mirroring the existing assertions at `stackslib/src/net/chat.rs:3625`-`3627`), and separately assert that no code path compares this URL's host against `handshake_data.addrbytes`/`port` before storing it (grep of `update_from_handshake_data` at `stackslib/src/net/chat.rs:1147` shows the direct clone with no check).
5. As a secondary confirmation, verify `PeerDB` row for this neighbor (post `neighbor.save_update`) stores the same unrelated URL, demonstrating persistence of the unvalidated value that `PeerNetwork::get_data_url` (`stackslib/src/net/download/epoch2x.rs:1060`) will later hand to the Atlas downloader for real DNS/HTTP requests.

### Citations

**File:** stackslib/src/net/chat.rs (L1141-1148)
```rust
        self.peer_version = preamble.peer_version;
        self.peer_network_id = preamble.network_id;
        self.peer_services = handshake_data.services;
        self.peer_expire_block_height = handshake_data.expire_block_height;
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();

```

**File:** stackslib/src/net/chat.rs (L1275-1286)
```rust
        if updated && self.stats.outbound {
            // save the new key
            let tx = network.peerdb_tx_begin().map_err(net_error::DBError)?;
            let (mut neighbor, _) = Neighbor::load_and_update(
                &tx,
                message.preamble.peer_version,
                message.preamble.network_id,
                &handshake_data,
            )?;
            neighbor.save_update(&tx, None)?;
            tx.commit()
                .map_err(|e| net_error::DBError(db_error::SqliteError(e)))?;
```

**File:** stackslib/src/net/chat.rs (L2780-2820)
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
```

**File:** stackslib/src/net/neighbors/db.rs (L522-539)
```rust
    fn save_neighbor_from_handshake(
        &self,
        network: &mut PeerNetwork,
        preamble: &Preamble,
        data: &HandshakeAcceptData,
        db_data: Option<&StackerDBHandshakeData>,
    ) -> Result<Neighbor, net_error> {
        let tx = network.peerdb_tx_begin()?;
        let (mut neighbor_from_handshake, _) = Neighbor::load_and_update(
            &tx,
            preamble.peer_version,
            preamble.network_id,
            &data.handshake,
        )?;
        neighbor_from_handshake.save_update(&tx, db_data.map(|x| x.smart_contracts.as_slice()))?;
        tx.commit()?;
        Ok(neighbor_from_handshake)
    }
```

**File:** stackslib/src/net/download/epoch2x.rs (L1060-1075)
```rust
    /// Get the data URL for a neighbor
    pub fn get_data_url(&self, neighbor_key: &NeighborKey) -> Option<UrlString> {
        match self.events.get(neighbor_key) {
            Some(event_id) => match self.peers.get(event_id) {
                Some(convo) => {
                    if convo.data_url.is_empty() {
                        None
                    } else {
                        Some(convo.data_url.clone())
                    }
                }
                None => None,
            },
            None => None,
        }
    }
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

**File:** stackslib/src/net/atlas/download.rs (L684-720)
```rust
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
