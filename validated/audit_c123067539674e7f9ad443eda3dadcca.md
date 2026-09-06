### Title
Peer-controlled `data_url` in Handshake enables SSRF-style outbound requests to attacker-chosen hosts - (File: `stackslib/src/net/chat.rs`, `stackslib/src/net/neighbors/rpc.rs`)

### Summary
`HandshakeData.data_url`, a field fully controlled by the remote peer, is stored on the connection state without any validation of the target host/address, and is later used as the destination for outbound HTTP requests issued by the node itself (StackerDB RPC fetches and mempool sync). Unlike the `addrbytes`/`port` fields, which are cross-checked against the actual socket address for outbound connections, `data_url` is accepted verbatim.

### Finding Description
During handshake processing, `validate_handshake` only validates `handshake_data.addrbytes`/`port` against the observed socket address for outbound connections: [1](#0-0) 

`data_url` is never checked here. It is then copied unconditionally into the conversation state: [2](#0-1) 

This `data_url` (and its DNS-resolved address, `data_ip`) is later used by `NeighborRPC::send_request` to actually connect out and issue an authenticated-looking Stacks HTTP request, with **no check at all** on whether the resolved address is private/internal: [3](#0-2) 

Contrast this with the mempool-sync code path, which explicitly guards against this exact scenario by rejecting private-range addresses unless `private_neighbors` is configured: [4](#0-3) 

No equivalent guard exists in `NeighborRPC::send_request` / `neighbors/rpc.rs`, which backs StackerDB chunk-fetch RPCs (`net/stackerdb/sync.rs` uses this path to query peers for chunks). Because any remote peer — inbound or outbound, unauthenticated with respect to any special privilege beyond completing a handshake — can set `data_url` to any string that parses as an HTTP URL/host, the node can be induced to make outbound TCP/HTTP requests to arbitrary targets (internal services, link-local ranges, other nodes it wouldn't otherwise contact), analogous to RSSHub's unvalidated-URL SSRF (GHSA-3p3p-cgj7-vgw3): the peer supplies a URL, and the node blindly issues a GET/POST to it.

### Impact Explanation
This is a High-severity issue under the given rubric ("bounded compute DoS on a read endpoint" / probing internal network reachability), since:
- It allows a remote, unprivileged peer to make the victim node connect to attacker-chosen hosts/ports (internal service discovery/port-scanning via response-timing/success signals).
- It can be leveraged to redirect the node's outbound requests at other nodes' data endpoints "as" this node, or to force the victim to spend connection/timeout budget hitting arbitrary hosts (mild DoS amplification), though this is bounded per-request and only reachable through the (StackerDB-only) `NeighborRPC` path, not the volumetric flooding case that is explicitly out of scope.
- It does not achieve unauthenticated state writes or forged-data propagation by itself; the requests are legitimate Stacks-protocol requests, and this path (unlike mempool sync) has no comment/design note suggesting private ranges were deliberately allowed — it looks like a missing check rather than an intentional exception.

### Likelihood Explanation
Likelihood is High: any peer can trivially set an arbitrary string as its handshake `data_url` (there is no format restriction beyond URL parseability), and outbound handshakes from the node's own perspective don't validate that field at all. The `NeighborRPC` path is exercised whenever the node performs StackerDB chunk-fetch operations against a peer that advertised the malicious `data_url`.

### Recommendation
Apply the same private/link-local-range check used in `mempool_sync_send_query`/`mempool_sync_pick_outbound_peer` (`PeerAddress::from_socketaddr(addr).is_in_private_range()`, gated by `connection_opts.private_neighbors`) inside `NeighborRPC::send_request` (and any other call site of `connect_http`/`connect_or_send_http_request` that resolves peer-supplied `data_url`s) before connecting. Consider also validating `data_url`'s host against `handshake_data.addrbytes`/observed peer IP when the connection is outbound, consistent with the existing address/port validation in `validate_handshake`.

### Proof of Concept
1. Attacker's node completes a P2P handshake with the victim (either as an inbound or outbound neighbor), sending `HandshakeData` with `data_url` set to an internal address, e.g. `http://169.254.169.254/` or `http://10.0.0.5:22/`.
2. `validate_handshake` (stackslib/src/net/chat.rs) accepts the handshake because it only checks `addrbytes`/`port`, not `data_url`.
3. `update_from_handshake_data` stores the attacker-chosen `data_url` on the conversation (`self.data_url = handshake_data.data_url.clone()`).
4. When the node later performs a StackerDB sync and needs to fetch a chunk from this "neighbor" via `NeighborRPC::send_request`, it resolves `data_url` and calls `connect_http`, issuing an HTTP request to the attacker-chosen internal target with no private-range check — unlike the mempool-sync path, which would have blocked this.

### Citations

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

**File:** stackslib/src/net/chat.rs (L1145-1147)
```rust
        self.handshake_addrbytes = handshake_data.addrbytes.clone();
        self.handshake_port = handshake_data.port;
        self.data_url = handshake_data.data_url.clone();
```

**File:** stackslib/src/net/neighbors/rpc.rs (L191-241)
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
