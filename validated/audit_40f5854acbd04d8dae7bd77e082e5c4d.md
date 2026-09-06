### Title
SSRF-style analog: unvalidated peer-supplied `data_url` (missing link-local/`169.254.0.0/16` filtering) lets a malicious peer make the node issue outbound HTTP requests to attacker-chosen internal/metadata addresses - ([File: stacks-common/src/types/net.rs])

### Summary
Stacks nodes learn a remote peer's HTTP "data URL" from the `Handshake`/`HandshakeAccept` P2P message and later dial that URL directly for StackerDB chunk sync, mempool sync, and Nakamoto tenure downloads. The only sanitization applied anywhere in this pipeline is `PeerAddress::is_in_private_range()`, which is missing the link-local range `169.254.0.0/16` — the exact range abused in the GeoNode SSRF report to reach cloud metadata endpoints (`169.254.169.254`). A remote, unauthenticated/low-trust peer can therefore set its advertised `data_url` to a link-local address and cause the victim node to make outbound HTTP requests to that address on the node operator's behalf.

### Finding Description
`HandshakeData.data_url` is taken verbatim from a peer-controlled message and stored with no address-class validation: [1](#0-0) 

The only defensive substitution that exists is in the neighbor-walk handshake-accept handler, and it only fires when the reported address `is_in_private_range()` or `is_anynet()`: [2](#0-1) 

`is_in_private_range()` itself only covers RFC1918 ranges (`10/8`, `172.16/12`, `192.168/16`) plus loopback `127/8` (and `fc00::/7`/`::1` for v6) — it never checks `169.254.0.0/16` (link-local / cloud-metadata range): [3](#0-2) 

This `data_url` is later used, unfiltered, to drive real outbound TCP/HTTP connections:
- `NeighborRPC::get_peer_host`/`send_request` build a `PeerHost` from `convo.data_url` and call `connect_http`, which opens a raw socket to the resolved address — used by the Nakamoto tenure downloader and StackerDB chunk-sync state machine: [4](#0-3) [5](#0-4) 
- `StackerDBSync::getchunks_begin` dispatches `StackerDBGetChunk` fetches to neighbors resolved this way: [6](#0-5) 
- Mempool sync does perform an `is_in_private_range()` gate before issuing its query, but because that predicate omits `169.254.0.0/16`, the gate does not stop link-local targets: [7](#0-6) 

By contrast, the StackerDB smart-contract "hint-replicas" path (config.rs) *does* call `is_in_private_range()` before trusting a contract-supplied replica address, showing the codebase's own intent to filter internal targets — but the filter predicate itself is incomplete, and the P2P-handshake-derived `data_url` path applies no equivalent contract-side scrutiny at all.

### Impact Explanation
A malicious peer can force a victim Stacks node to originate outbound HTTP requests toward `169.254.0.0/16` addresses reachable from the node's host/container (most notably cloud instance-metadata services on IaaS providers, or other link-local services), using the node as a blind SSRF proxy. This lets an attacker fingerprint/port-scan internal infrastructure reachable only from the node's network position, mirroring the exact "port scan internal hosts" impact described in the GeoNode advisory. It does not directly leak response bodies back to the attacker (the node validates/discards StackerDB/tenure/mempool responses rather than echoing them), so this is a "blind" SSRF rather than a full data-exfiltration SSRF, but it is a genuine confused-deputy issue: a value asserted by an untrusted remote peer is trusted as a safe outbound HTTP target class, breaking the intended "no requests to internal/link-local ranges" invariant that the codebase enforces inconsistently elsewhere.

### Likelihood Explanation
Any node that accepts inbound P2P connections (default configuration) will process `Handshake`/`HandshakeAccept` messages from arbitrary remote peers and store their `data_url` verbatim. No signature check or allow-list restricts the URL's host beyond the incomplete private-range filter. Triggering an outbound request requires no privileged capability — establishing a normal P2P handshake and subsequently participating in StackerDB replication, tenure download, or mempool sync is sufficient. The DNS/plain-IP resolution path (`try_decode_data_url_ipaddr`, `chat.rs`) accepts literal IPv4/IPv6 addresses directly, so no DNS rebinding trick is even required — a literal `169.254.169.254` data URL suffices.

### Recommendation
- Extend `PeerAddress::is_in_private_range()` (or add a dedicated `is_link_local()`/`is_metadata_range()` check) to also cover `169.254.0.0/16` (and its IPv6 analog `fe80::/10`), and apply this filter consistently to every code path that dials a peer/DB-replica-supplied `data_url` (chat.rs handshake processing, neighbors/rpc.rs, mempool/mod.rs, stackerdb/sync.rs), not just the smart-contract hint-replicas path.
- Reject/ignore handshake `data_url`s whose resolved address falls in a disallowed range at the point they are stored (`update_from_handshake_data`), rather than only compensating for it later in one caller (`neighbor_walk.rs`).
- Consider applying the same disallowed-range check after DNS resolution as well (`chat.rs` `resolve_data_url`/DNS lookup path), since a peer could supply a routable-looking hostname that resolves to a link-local/private address via DNS.

### Proof of Concept
1. Stand up a malicious Stacks P2P peer that completes a valid handshake with a target node, but sets `HandshakeData.data_url = "http://169.254.169.254/latest/meta-data/"` (or any other 169.254.0.0/16 target reachable from the victim node's environment).
2. Ensure this peer also advertises `StackerDBHandshakeData` for a smart contract the node is configured to replicate (or simply wait for the target node's Nakamoto tenure-download / mempool-sync logic to select this neighbor).
3. Observe that the target node's `ConversationHttp`/`NeighborRPC` layer opens an outbound TCP/HTTP connection to `169.254.169.254` (visible via network capture or via the target's debug logs showing `"HTTP event {event_id} connected"` / `send_request` against that address), confirming the node followed the attacker-supplied internal address without rejection.

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

**File:** stackslib/src/net/neighbors/walk.rs (L689-703)
```rust
        // if the neighbor accidentally gave us a private IP address, then
        // just use the one we used to contact it.  This can happen if the
        // node is behind a load-balancer, or is doing port-forwarding,
        // etc. But do nothing if both cur_neighbor and its reported address are private.
        if (neighbor_from_handshake.addr.addrbytes.is_in_private_range()
            || neighbor_from_handshake.addr.addrbytes.is_anynet())
            && !self.cur_neighbor.addr.addrbytes.is_in_private_range()
        {
            debug!(
                "{}: outbound neighbor gave private IP address {:?}; assuming it meant {:?}",
                local_peer_str, &neighbor_from_handshake.addr, &self.cur_neighbor.addr
            );
            neighbor_from_handshake.addr.addrbytes = self.cur_neighbor.addr.addrbytes.clone();
            neighbor_from_handshake.addr.port = self.cur_neighbor.addr.port;
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

**File:** stackslib/src/net/neighbors/rpc.rs (L182-233)
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
```

**File:** stackslib/src/net/server.rs (L118-149)
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
```

**File:** stackslib/src/net/stackerdb/sync.rs (L1019-1057)
```rust
    pub fn getchunks_begin(&mut self, network: &mut PeerNetwork) -> Result<bool, net_error> {
        if self.chunk_fetch_priorities.is_empty() {
            // done
            debug!(
                "{:?}: {}: getchunks_begin: no chunks prioritized",
                network.get_local_peer(),
                &self.smart_contract_id
            );
            return Ok(true);
        }

        let mut cur_priority = self.next_chunk_fetch_priority % self.chunk_fetch_priorities.len();

        debug!(
            "{:?}: {}: getchunks_begin: Issue up to {} StackerDBGetChunk requests",
            &network.get_local_peer(),
            &self.smart_contract_id,
            self.request_capacity;
            "chunk_fetch_priorities" => ?self.chunk_fetch_priorities,
        );

        let mut requested = 0;
        let mut unpin = HashSet::new();

        // fill up our comms with $capacity requests
        for _i in 0..self.request_capacity {
            if self.comms.count_inflight() >= self.request_capacity {
                break;
            }
            let cur_fetch_priority = self
                .chunk_fetch_priorities
                .get_mut(cur_priority)
                .ok_or_else(|| {
                    error!(
                        "Error setting chunk fetch priories. Priority index out of bounds";
                        "cur_priority" => cur_priority,
                    );
                    net_error::InvalidState
                })?;
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
