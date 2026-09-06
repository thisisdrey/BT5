## Analysis Result

### Title
Attacker-controlled hostname in P2P Handshake `data_url` can stall the single shared DNS resolver thread, blocking data-URL resolution node-wide - (File: `stackslib/src/net/dns.rs`, `stackslib/src/net/chat.rs`)

### Summary
The Keycloak advisory describes a DoS caused by triggering costly, unvalidated DNS resolution from untrusted client-controlled input, tying up limited threads. `stacks-core` has a structurally analogous, but weaker, issue: the P2P handshake's `data_url` field is fully attacker-controlled and unvalidated, and is fed into a single, node-wide, blocking DNS resolver thread shared by all subsystems (handshake data-URL resolution, mempool sync, Atlas downloads, epoch2x block downloads).

### Finding Description
`HandshakeData::data_url` is deserialized directly off the wire with no restriction on its host content (only length/ASCII URL syntax): [1](#0-0) . Any remote, unauthenticated peer sending a `Handshake` message can set this to an arbitrary domain name, and `update_from_handshake_data` copies it verbatim into the conversation state before authentication trust is otherwise established: [2](#0-1) .

When the node later tries to resolve this domain (`try_decode_data_url_ipaddr` fails for non-IP hosts), it queues a lookup on the shared `DNSClient`/`DNSResolver`: [3](#0-2) .

The `DNSResolver` is a single background thread, created once per node with a small `max_inflight` (10 in production), and used for *all* domain resolution paths — handshake data URLs, mempool sync peer URLs, Atlas attachment URLs, and epoch2x block-download URLs: [4](#0-3) . Its core `resolve()` method performs a fully blocking `to_socket_addrs()` call with no application-level timeout, relying entirely on the OS resolver's own (potentially very long or unbounded) timeout behavior: [5](#0-4) . The `thread_main` loop processes queued requests strictly sequentially, one at a time, via `handle_query`: [6](#0-5) .

Because there is only one resolver thread for the whole node, a single attacker-supplied domain name that resolves slowly (e.g., pointed at a non-responsive or intentionally slow authoritative DNS server) will occupy the resolver thread for the duration of the OS-level lookup, blocking resolution of every other pending/future lookup — including legitimate peers' handshake `data_url`s, mempool sync target resolution, Atlas attachment fetches, and epoch2x block download URL resolution — until that one lookup completes or the local application-level deadline check causes the *client* to give up (the request itself is not cancelled server-side; the resolver thread is still blocked in the OS call).

### Impact Explanation
This breaks the equality that "DNS resolution capacity is available for legitimate node needs" — a single attacker-chosen slow-to-resolve hostname (delivered via an unauthenticated Handshake) can monopolize the sole DNS worker thread, starving unrelated concurrent DNS needs (mempool sync progress, Atlas attachment sync, epoch2x block download peer resolution, and other peers' handshake data-URL resolution) for as long as the OS resolver call is outstanding. This is a bounded compute/read-path DoS reachable with a handful of messages from an unauthenticated connection, matching the "High" impact category (bounded compute DoS affecting shared, read-adjacent resolution).

### Likelihood Explanation
Likelihood is moderate: no privileged position or secret key is required — any peer that can open a P2P connection can send a `Handshake` with an attacker-chosen `data_url` domain. However, actually causing the OS resolver to hang for a long duration typically requires control (or influence) over an authoritative DNS server for the attacker's domain (e.g., configuring it to be unresponsive rather than returning NXDOMAIN), which is achievable by any attacker who owns a domain and its nameserver. The effect is also somewhat self-limiting since queued/expired requests are eventually marked timed out client-side (`is_timed_out`), but this does not un-block the resolver thread that is stuck inside the blocking OS call.

### Recommendation
- Enforce an explicit, short timeout on the DNS resolution call itself (e.g., spawn the blocking `to_socket_addrs()` in a bounded-lifetime helper thread/future with a hard deadline, rather than relying on OS defaults) in `DNSResolver::resolve` (`stackslib/src/net/dns.rs`).
- Consider running multiple DNS worker threads (a small pool) rather than a single serialized resolver thread, so one slow lookup cannot block all others.
- Optionally, validate/deprioritize non-IP `data_url` hosts received from not-yet-trusted/unauthenticated peers, or apply a per-neighbor rate limit on triggering DNS lookups from handshake data.

### Proof of Concept
1. Register or control a domain (e.g., `slow.attacker.example`) whose authoritative nameserver is configured to never respond to queries (black-hole UDP/TCP port 53).
2. Open a P2P connection to a target Stacks node and send a `Handshake` message with `data_url = "http://slow.attacker.example:80/"`.
3. Once the node processes the handshake and calls `resolve_data_url` (chat.rs) it queues a lookup on the shared `DNSClient`; the single `DNSResolver` thread picks it up and calls the blocking `to_socket_addrs()` (`dns.rs::resolve`), which stalls for the OS-level DNS timeout (which can be tens of seconds to minutes depending on OS/network stack configuration).
4. During this stall, legitimate concurrent DNS-dependent operations (other peers' handshake resolution, mempool sync, Atlas downloads, epoch2x block-download peer resolution) queued on the same resolver are delayed or dropped ("Too many DNS requests in-flight") until the stalled lookup completes.

### Citations

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

**File:** stackslib/src/net/chat.rs (L2867-2892)
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
```

**File:** stacks-node/src/nakamoto_node/peer.rs (L79-91)
```rust
        let (mut dns_resolver, mut dns_client) = DNSResolver::new(10);

        // spawn a daemon thread that runs the DNS resolver.
        // It will die when the rest of the system dies.
        {
            let _jh = thread::Builder::new()
                .name("dns-resolver".to_string())
                .spawn(move || {
                    debug!("DNS resolver thread ID is {:?}", thread::current().id());
                    dns_resolver.thread_main();
                })
                .unwrap();
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

**File:** stackslib/src/net/dns.rs (L193-241)
```rust
    pub fn handle_query(&mut self) -> Option<DNSResponse> {
        let req = match self.queries.pop_front() {
            Some(r) => r,
            None => {
                return None;
            }
        };

        if req.is_timed_out() {
            return Some(DNSResponse::error(req, "DNS request timed out".to_string()));
        }

        let resp = self.resolve(req);
        Some(resp)
    }

    pub fn thread_main(&mut self) {
        test_debug!("DNS start");
        loop {
            // prime the pump, or die if the inbound channel is broken
            match self.drain_inbox() {
                Ok(count) => {
                    if count == 0 {
                        sleep_ms(100);
                    }
                }
                Err(_e) => {
                    test_debug!("Failed to drain DNS inbox; exiting");
                    break;
                }
            }

            for _ in 0..self.max_inflight {
                let resp = match self.handle_query() {
                    Some(r) => r,
                    None => {
                        // out of requests
                        break;
                    }
                };

                if let Err(TrySendError::Disconnected(_)) = self.outbound.try_send(resp) {
                    test_debug!("DNS client disconnected; exiting");
                    break;
                }
            }
        }
        test_debug!("DNS join");
    }
```
