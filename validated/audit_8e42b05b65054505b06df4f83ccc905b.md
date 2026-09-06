### Title
Authenticated SSRF via attacker-supplied `data_url` in P2P Handshake messages - (File: stackslib/src/net/chat.rs)

### Summary
A remote peer that completes a normal (permissionless) Stacks P2P handshake can set an arbitrary `data_url` in its `HandshakeData`. This URL is stored verbatim on the victim node and is later dereferenced by multiple subsystems (block/microblock download, Atlas attachment sync, StackerDB neighbor RPC) to open outbound TCP/HTTP connections, with no validation that the host is not a loopback, link-local, or internal/private address. This mirrors the AVideo `downloadURL` SSRF pattern: a low-privilege, "authenticated" (here: handshake-completed) actor supplies a URL that the server fetches on their behalf.

### Finding Description
When a `Handshake` message is processed, the peer-supplied `data_url` is copied directly into the conversation state with no host/IP validation: [1](#0-0) 

The only checks performed on the handshake (`validate_handshake`) concern the signature, the sender's public key, and its self-reported `addrbytes`/`port` matching the socket address for outbound connections — none of these validate the *content* of `data_url`: [2](#0-1) 

This `data_url` is subsequently treated as a trusted "where can I fetch data from this neighbor" endpoint across several subsystems:
- Block/microblock downloader builds `BlockRequestKey`s directly from `self.get_data_url(&nk)` with no host filtering: [3](#0-2) 
- DNS resolution of these URLs happens with no restriction on resolved IP ranges, then a raw HTTP request is opened to whatever address results: [4](#0-3) 
- Atlas attachment sync collects `peer_url` from `network.get_data_url(&peer)` for every outbound sync peer and uses it to construct real HTTP requests for inventories/attachments: [5](#0-4) 
- StackerDB/neighbor RPC layer resolves `PeerHost` straight from `convo.data_url` and issues an HTTP request to it: [6](#0-5) 

The root cause equality broken is: "peer-claimed reachable endpoint" is treated as equal to "endpoint that is safe/appropriate for this node to make outbound HTTP requests to." No code path validates that the host encoded in `data_url` is not `127.0.0.1`, a link-local/metadata address (e.g. `169.254.169.254`), or an otherwise internal-only address before the node performs a real TCP connect and HTTP request against it.

### Impact Explanation
Any remote node that can complete a handshake (a low-bar, largely permissionless action in this P2P protocol — it only requires a valid keypair and a compatible network/peer version, not an admin credential or node secret) can force the victim node to make outbound HTTP requests to attacker-chosen hosts, including internal-only services, loopback, or cloud metadata endpoints. Depending on deployment (e.g., colocated RPC/admin ports, internal metadata services, cloud instance metadata), this could allow network reconnaissance, or interaction with internal services that trust connections from the node's own host, closely matching the CWE-918 impact of the analogous AVideo advisory.

### Likelihood Explanation
High likelihood in a permissionless deployment: any peer capable of forming a valid P2P handshake (the routine, expected operation for joining the network) can supply the `data_url`, and the victim will use it during ordinary sync activities (Atlas attachment fetch, block download, StackerDB RPC) without any operator action required. No special role, node secret, or admin access is needed — only participation as a peer.

### Recommendation
- Validate `data_url` on receipt in `update_from_handshake_data` (and anywhere else a peer-reported URL is accepted) by resolving the host and rejecting loopback, link-local (169.254.0.0/16, fe80::/10), private RFC1918/RFC4193 ranges, and any address matching the node's own listening interfaces, unless explicitly configured to allow such peers (e.g., local/regtest testing mode).
- Perform the same validation immediately before dialing out in `connect_or_send_http_request` / `send_http_request`, so that even DNS rebinding after initial validation cannot bypass the check.
- Optionally, only trust `data_url` for peers on an operator-configured allow-list, or require it to match the already-verified handshake `addrbytes`/`port`, closing the gap where advertised `data_url` diverges from the peer's verified network identity.

### Proof of Concept
1. Attacker runs a node (or a raw client implementing the Stacks P2P wire protocol) with valid keys, and initiates a handshake to the victim node, setting `HandshakeData.data_url` to `http://169.254.169.254/latest/meta-data/` (or `http://127.0.0.1:<internal-port>/...`) instead of its own real reachable address.
2. Victim processes the handshake; `ConversationP2P::update_from_handshake_data` stores this value unmodified into `self.data_url` (`stackslib/src/net/chat.rs:1147`), with no host validation.
3. During normal operation (e.g., Atlas attachment resolution picking this peer via `network.get_outbound_sync_peers()`/`get_data_url`, or the block downloader building `BlockRequestKey`s), the victim resolves this URL's host via DNS and opens a real outbound TCP connection, sending an HTTP GET/POST to the attacker-chosen internal target (`stackslib/src/net/httpcore.rs:1942-1982`).
4. The attacker observes side effects (timing, error/Nack behavior, or reachability differences) to probe internal network reachability from the victim's host — the same class of impact as the original AVideo `downloadURL` SSRF.

### Citations

**File:** stackslib/src/net/chat.rs (L1047-1127)
```rust
    fn validate_handshake(
        &mut self,
        local_peer: &LocalPeer,
        chain_view: &BurnchainView,
        message: &mut StacksMessage,
    ) -> Result<(), net_error> {
        let handshake_data = match message.payload {
            StacksMessageType::Handshake(ref mut data) => data.clone(),
            _ => panic!("Message is not a handshake"),
        };

        match self.connection.get_public_key() {
            None => {
                // if we don't yet have a public key for this node, verify the message.
                // if it's improperly signed, it's probably a poorly-timed re-key request (but either way the message should be rejected)
                message
                    .verify_secp256k1(&handshake_data.node_public_key)
                    .map_err(|_e| {
                        debug!(
                            "{:?}: invalid handshake: not signed with given public key",
                            &self
                        );
                        net_error::InvalidMessage
                    })?;
            }
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
        };

        let their_public_key_res = handshake_data.node_public_key.to_public_key();
        match their_public_key_res {
            Ok(_) => {}
            Err(_e) => {
                // bad public key
                debug!("{:?}: invalid handshake -- invalid public key", &self);
                return Err(net_error::InvalidMessage);
            }
        };

        if handshake_data.expire_block_height <= chain_view.burn_block_height {
            // already stale
            debug!(
                "{:?}: invalid handshake -- stale public key (expired at {})",
                &self, handshake_data.expire_block_height
            );
            return Err(net_error::InvalidHandshake);
        }

        // the handshake cannot come from us
        if handshake_data.node_public_key
            == StacksPublicKeyBuffer::from_public_key(&Secp256k1PublicKey::from_private(
                &local_peer.private_key,
            ))
        {
            debug!(
                "{:?}: invalid handshake -- got a handshake from myself",
                &self
            );
            return Err(net_error::InvalidHandshake);
        }

        Ok(())
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

**File:** stackslib/src/net/download/epoch2x.rs (L1453-1468)
```rust
                let Some(data_url) = self.get_data_url(&nk) else {
                    debug!(
                        "{:?}: Unable to request {} from {}: no data URL",
                        &self.local_peer, &target_index_block_hash, &nk
                    );
                    continue;
                };
                if data_url.is_empty() {
                    // peer doesn't yet know its public IP address, and isn't given a data URL
                    // directly
                    debug!(
                        "{:?}: Unable to request {} from {}: no data URL",
                        &self.local_peer, &target_index_block_hash, &nk
                    );
                    continue;
                }
```

**File:** stackslib/src/net/httpcore.rs (L1877-1911)
```rust
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

**File:** stackslib/src/net/atlas/download.rs (L107-124)
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
```

**File:** stackslib/src/net/neighbors/rpc.rs (L182-228)
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
```
