## Analysis

The hackney CVE's core defect is **unbounded, permanent accumulation of attacker-controlled keys in a shared table with no eviction path once the originating request is abandoned**. The closest analog in-scope is not an atom table (Rust has none), but the shared `DNSClient.requests` map in `stackslib/src/net/dns.rs`, which is populated with attacker-controlled hostnames taken from a remote peer's self-advertised `data_url` and is only ever cleaned up by the very conversation that queued it — never by peer disconnection/eviction.

### Title
Unbounded permanent growth of the shared `DNSClient` request table via peer-supplied `data_url` hostnames that are never reclaimed on disconnect - (File: stackslib/src/net/dns.rs)

### Summary
`DNSClient::queue_lookup` unconditionally inserts a new entry into `self.requests: HashMap<DNSRequest, Option<DNSResponse>>` keyed by `(host, port)`, where `host` is taken directly from a remote peer's handshake-advertised `data_url`. The only code path that removes an entry is `poll_lookup`, which is called solely by the same `ConversationP2P` that originally issued the lookup. If that peer disconnects, is pruned, or is banned before its own polling logic revisits the request, the entry is orphaned permanently in the single, process-wide `DNSClient` instance used by the whole p2p thread.

### Finding Description
`DNSClient::queue_lookup` inserts into the shared map with no cap and no ownership tracking tying an entry back to a live conversation: [1](#0-0) 

The only removal path is `poll_lookup`, and even a timed-out request is merely overwritten in place (key retained) rather than removed: [2](#0-1) [3](#0-2) 

The hostname enqueued comes from the remote peer's own advertised `data_url`, resolved once per dispatch pass by `ConversationP2P::try_resolve_data_url_host`, which calls `dns_client.queue_lookup(domain, ...)` using the domain parsed out of the peer-supplied URL: [4](#0-3) 

This is driven every dispatch cycle for every live conversation from the single, shared `DNSClient` instance created once for the whole p2p thread: [5](#0-4) 

Critically, when a peer is deregistered (disconnected, pruned, or banned), `deregister_peer` cleans up `pending_messages`, `pending_stacks_messages`, sockets, `peers`, `relay_handles`, and inventory state — but never touches the `DNSClient`'s `requests` map: [6](#0-5) 

Since `DNSRequest`'s `Hash`/`PartialEq` are defined only over `(host, port)` and ignore `timeout`, each distinct attacker-chosen hostname produces a permanently-distinct key: [7](#0-6) 

An attacker who repeatedly connects, sends a `Handshake` advertising a unique never-before-seen hostname as `data_url`, and then disconnects before that specific conversation's next dispatch pass polls for it, leaves one permanent, unreclaimable entry in the shared `DNSClient.requests` map. This is structurally identical to the hackney bug class: an unbounded, attacker-controlled-key table that mints one permanent entry per unique attacker-chosen string, with no possible garbage collection once the originating context (BEAM caller / `ConversationP2P`) is gone.

### Impact Explanation
Repeating this indefinitely causes unbounded heap growth in the p2p thread's `DNSClient`, eventually leading to memory exhaustion and node crash/OOM — an unauthenticated, remote, low-per-request-cost denial of service, matching the "remote crash / unauthenticated DoS" impact tier. Unlike pure bandwidth-flooding, the cost to the attacker per leaked entry is a single connection + handshake with a new hostname, not sustained high-volume traffic.

### Likelihood Explanation
Likelihood is moderate-to-high: it requires no authentication, no valid signature, and no special node role — only the ability to complete a p2p handshake (which any peer can do) and disconnect before the node's DNS-resolution loop happens to poll that host again. Repeated automated connect/handshake/disconnect cycles with fresh subdomains (e.g., `*.attacker-domain.com`) are trivial to script and require negligible bandwidth per attempt.

### Recommendation
Track DNS request ownership per `event_id`/conversation and explicitly cancel/remove the corresponding `DNSRequest` entry from the shared `DNSClient.requests` map inside `deregister_peer` (or an equivalent cleanup hook). Additionally, enforce a hard upper bound on the number of outstanding/cached entries in `DNSClient.requests` (evicting oldest/timed-out entries), analogous to `DNSResolver::max_inflight`, so that no unbounded, permanently-orphaned entries can accumulate regardless of disconnect timing.

### Proof of Concept
1. Start a node under attacker control (or many ephemeral connections from one attacker).
2. For `i` in `0..N`: open a p2p connection, send a valid `Handshake` whose `data_url` domain is `host-<i>.attacker-controlled-domain.com` (a fresh, never-before-seen subdomain each time), then immediately close the TCP connection before the node's next dispatch cycle can call `poll_lookup` for that host.
3. Each iteration causes `try_resolve_data_url_host` → `DNSClient::queue_lookup` to insert one permanent entry into the shared `requests` map; because the conversation is torn down via `deregister_peer` (which never touches `DNSClient`), the entry can never be polled/removed.
4. Repeating this at scale drives `DNSClient.requests` to grow without bound over time, consuming node memory until OOM/crash.

### Citations

**File:** stackslib/src/net/dns.rs (L51-62)
```rust
impl Hash for DNSRequest {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.host.hash(state);
        self.port.hash(state);
    }
}

impl PartialEq for DNSRequest {
    fn eq(&self, other: &DNSRequest) -> bool {
        self.host == other.host && self.port == other.port
    }
}
```

**File:** stackslib/src/net/dns.rs (L253-260)
```rust
    pub fn queue_lookup(&mut self, host: &str, port: u16, timeout: u128) -> Result<(), net_error> {
        let req = DNSRequest::new(host.to_string(), port, timeout);
        self.requests_tx
            .send(req.clone())
            .map_err(|_se| net_error::LookupError("Failed to queue DNS query".to_string()))?;
        self.requests.insert(req, None);
        Ok(())
    }
```

**File:** stackslib/src/net/dns.rs (L262-276)
```rust
    fn clear_timeouts(&mut self) {
        let mut to_remove = vec![];
        for req in self.requests.keys() {
            if req.is_timed_out() {
                debug!("Lookup {}:{} timed out", &req.host, req.port);
                to_remove.push(req.clone());
            }
        }
        for req in to_remove.into_iter() {
            self.requests.insert(
                req.clone(),
                Some(DNSResponse::error(req, "DNS lookup timed out".to_string())),
            );
        }
    }
```

**File:** stackslib/src/net/dns.rs (L315-341)
```rust
    pub fn poll_lookup(&mut self, host: &str, port: u16) -> Result<Option<DNSResponse>, net_error> {
        let req = DNSRequest::new(host.to_string(), port, 0);
        if !self.requests.contains_key(&req) {
            return Err(net_error::LookupError(format!(
                "No such pending lookup: {}:{}",
                host, port
            )));
        }

        let _ = match self.requests.get(&req) {
            Some(None) => {
                return Ok(None);
            }
            Some(Some(resp)) => resp,
            None => {
                unreachable!();
            }
        };

        let resp = self
            .requests
            .remove(&req)
            .expect("BUG: had key but then didn't")
            .expect("BUG: had response but then didn't");

        Ok(Some(resp))
    }
```

**File:** stackslib/src/net/chat.rs (L2867-2911)
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
        if let Some(ip) = self.data_ip.as_ref() {
```

**File:** stackslib/src/net/mod.rs (L86-88)
```rust
/// Implements `DNSResolver`, a simple DNS resolver state machine. Also implements `DNSClient`,
/// which serves as an API for `DNSResolver`.
pub mod dns;
```

**File:** stackslib/src/net/p2p.rs (L2102-2169)
```rust
    /// Deregister a socket/event pair
    pub fn deregister_peer(&mut self, peer: DropPeer) {
        let reason = peer.reason;
        debug!(
            "{:?}: Disconnect peer {}:{}",
            &self.local_peer,
            peer.address.pretty_print(),
            peer.port,
        );

        let mut nk_remove = vec![];
        for (neighbor_key, event_id) in self.events.iter() {
            if neighbor_key.addrbytes == peer.address && neighbor_key.port == peer.port {
                let pubkh = self
                    .get_p2p_convo(*event_id)
                    .and_then(|convo| convo.get_public_key_hash())
                    .unwrap_or(Hash160([0x00; 20]));
                nk_remove.push((neighbor_key.clone(), pubkh));
            }
        }

        for (nk, pubkh) in nk_remove.into_iter() {
            // remove event state
            if let Some(event_id) = self.events.remove(&nk) {
                info!("Dropping neighbor!";
                    "event id" => event_id,
                    "public key" => %pubkh,
                    "public addr" => nk.addrbytes.pretty_print(),
                    "reason" => %reason
                );
                self.pending_messages.remove(&(event_id, nk.clone()));
                self.pending_stacks_messages.remove(&(event_id, nk.clone()));

                match self.network {
                    None => {}
                    Some(ref mut network) => {
                        // deregister socket if connected and registered already
                        if let Some(socket) = self.sockets.remove(&event_id) {
                            let _ = network.deregister(event_id, &socket);
                        }
                        // deregister socket if still connecting
                        if let Some(ConnectingPeer { socket, .. }) =
                            self.connecting.remove(&event_id)
                        {
                            let _ = network.deregister(event_id, &socket);
                        }
                    }
                }
                self.relay_handles.remove(&event_id);
                self.peers.remove(&event_id);
            }
            // remove inventory state
            if let Some(inv_state) = self.inv_state.as_mut() {
                debug!(
                    "{:?}: Remove inventory state for epoch 2.x {nk:?}",
                    &self.local_peer
                );
                inv_state.del_peer(&nk);
            }
            if let Some(inv_state) = self.inv_state_nakamoto.as_mut() {
                debug!(
                    "{:?}: Remove inventory state for Nakamoto {nk:?}",
                    &self.local_peer
                );
                inv_state.del_peer(&NeighborAddress::from_neighbor_key(nk.clone(), pubkh));
            }
        }
    }
```
