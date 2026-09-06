### Title
Confused Deputy / SSRF via Peer-Controlled `data_url` in Atlas Attachment Downloader (missing private-range check) - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
Any already-connected remote peer supplies its own `data_url` (an HTTP host:port string) as part of the p2p handshake. This value is later trusted by the Atlas attachment-sync subsystem to make outbound HTTP GET requests to fetch attachment inventories and attachment bodies, with **no check that the resolved address is non-private/non-internal**. This mirrors the Hystrix Dashboard `proxy.stream` confused-deputy bug: an attacker-controlled endpoint value causes the victim server to issue outbound requests to arbitrary hosts, including internal-only services.

### Finding Description
When Atlas syncs attachments, it builds its peer set directly from each outbound peer's advertised `data_url`: [1](#0-0) 

These URLs are later DNS-resolved and used to build outbound HTTP requests without any filtering of the resolved socket address against private/loopback/link-local ranges, unlike the mempool sync path which explicitly guards this: [2](#0-1) 

The general data-URL resolution logic in `ConversationP2P` (`chat.rs`) — which underlies `data_ip`/`data_url` used by both mempool sync and Atlas — decodes and DNS-resolves whatever string the remote peer advertised, again with no private-range gate: [3](#0-2) 

The peer-registration path (`can_register_peer`) only rejects private-range addresses for the raw P2P TCP connection, not for the independently-attacker-supplied `data_url` string used for the HTTP side-channel: [4](#0-3) 

Because Atlas's attachment-inventory and attachment-fetch requests (`AttachmentsInventoryRequest`, `AttachmentRequest`) are built and dispatched using this same unguarded `data_url` → DNS → socket-address pipeline, the node is fooled into acting as an outbound HTTP client toward whatever host the remote peer names — including addresses in `127.0.0.0/8`, `169.254.0.0/16`, `10.0.0.0/8`, etc. — exactly the "make requests to any server reachable by the server hosting the dashboard" confused-deputy pattern in the reference advisory.

### Impact Explanation
A remote, unprivileged peer (only requires completing the ordinary p2p handshake, no admin role or node secret) can steer a victim Stacks node into issuing outbound HTTP requests to internal/private network endpoints reachable from that node (e.g., internal management APIs, cloud metadata services, or other services on the node's private network) by simply advertising a malicious `data_url`. This is a network-reachable confused deputy / SSRF condition consistent with CWE-441/CWE-610, and can be used for internal network reconnaissance or to trigger unwanted requests against internal services from the node's IP.

### Likelihood Explanation
Likelihood is moderate-to-high: any peer that becomes an authenticated, outbound Atlas sync peer (a routine, unprivileged state reached during normal p2p operation) can set an arbitrary `data_url` and be selected by `get_outbound_sync_peers`/Atlas batch peer selection with no additional preconditions. The fact that an equivalent guard (`is_in_private_range`) already exists for the mempool-sync code path but is absent from the Atlas/`chat.rs` data-URL resolution path indicates this is an overlooked gap rather than an intentionally accepted risk.

### Recommendation
Apply the same private/local-range filtering used in `mempool_sync_send_query` (`PeerAddress::from_socketaddr(addr).is_in_private_range()` gated by `connection_opts.private_neighbors`) to every consumer of a peer-supplied `data_url`, specifically:
- The DNS/IP resolution result inside `ConversationP2P` (`chat.rs`, the `data_ip` resolution logic around lines 2805–2910).
- The Atlas `AttachmentsDownloader`/`AttachmentsBatchStateContext` request-building path in `stackslib/src/net/atlas/download.rs` before enqueuing `AttachmentsInventoryRequest`/`AttachmentRequest`.
- The generic `NeighborRPC::get_peer_host`/`send_request` path in `stackslib/src/net/neighbors/rpc.rs`.

### Proof of Concept
1. Stand up a malicious peer that completes the normal Stacks p2p handshake with a target node, advertising `data_url = "http://127.0.0.1:<internal-port>/"` (or another private/internal address reachable from the victim node).
2. Wait until the target selects this peer as an outbound Atlas sync peer (`network.get_outbound_sync_peers()`); this only requires normal peer-walk/handshake success.
3. Trigger (or wait for) an Atlas attachment batch to be processed; the target's `AttachmentsBatchStateMachine` will DNS-resolve the attacker's `data_url` and issue `GET /v2/attachments/inv` / `GET /v2/attachments/<hash>` HTTP requests to the attacker-specified address, none of which is checked against private IP ranges, unlike the equivalent mempool-sync guard.
4. Observe (e.g., via a local listener at the private address) that the victim node connects and issues an HTTP request to the internal target — confirming SSRF/confused-deputy behavior.

### Citations

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

**File:** stackslib/src/net/chat.rs (L2805-2866)
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

**File:** stackslib/src/net/p2p.rs (L1917-1924)
```rust
        // unroutable?
        if !self.connection_opts.private_neighbors && neighbor_key.addrbytes.is_in_private_range() {
            debug!("{:?}: Peer {:?} is in private range and we are configured to drop private neighbors",
                  &self.local_peer,
                  neighbor_key
            );
            return Err(net_error::Denied);
        }
```
