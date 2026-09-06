### Title
Unauthenticated leak of the local peer's un-filtered address via `RPCListStackerDBReplicasRequestHandler::try_handle_request` insert bypassing the private/anynet filter - (File: stackslib/src/net/api/liststackerdbreplicas.rs)

### Summary
`RPCListStackerDBReplicasRequestHandler` performs no authentication whatsoever, and its only access control (`allow_private`/`is_in_private_range`/`is_anynet` filtering) is applied solely to the `PeerDB::find_stacker_db_replicas` results, not to the local peer's own entry that is unconditionally inserted at index 0. If the node's bound address is a private-range/unroutable address and its public IP has not yet been confirmed, that private address is served verbatim to any unauthenticated remote caller who supplies a `contract_identifier` present in `local_peer.stacker_dbs`.

### Finding Description
`try_parse_request` [1](#0-0)  only validates the URL shape and content-length; there is no `auth` field on `RPCListStackerDBReplicasRequestHandler` [2](#0-1)  and no secret/header check anywhere in the handler.

`try_handle_request` fetches replicas from `PeerDB::find_stacker_db_replicas` and applies the filter (`is_anynet` exclusion, and private-range exclusion unless `allow_private`) [3](#0-2) . This filter is the only access control in the endpoint. However, immediately after, if the requested `contract_identifier` is one the local node hosts, `local_peer.to_public_neighbor_addr()` is unconditionally inserted at index 0 with no filter applied at all: [4](#0-3) .

`LocalPeer::to_public_neighbor_addr` is a "best-effort" function: it only returns the confirmed public IP if `public_ip_address` is `Some(..)`; otherwise it falls back to `self.addrbytes`/`self.port` — the node's raw bind address — via `to_neighbor_addr()`: [5](#0-4) . `public_ip_address` starts as `None` and is only populated after a successful NAT-punch exchange with a peer [6](#0-5) , and is reset to `None` again whenever the IP query state resets [7](#0-6) . So there is a real, reachable window (startup, behind NAT, no public-IP peers yet responded, or a private/loopback-address node) in which `to_public_neighbor_addr()` returns the node's private/internal `addrbytes`.

This breaks the claimed equality: "addresses served == addresses passing the private/anynet filter." The local peer's own address is served regardless of `allow_private` and regardless of whether it is a private-range or anynet address, because the `insert(0, ...)` runs after and outside the filter closure that only applies to `PeerDB` results.

### Impact Explanation
An unauthenticated remote caller who knows (or guesses) a `QualifiedContractIdentifier` present in the target node's `local_peer.stacker_dbs` (StackerDB contract names, e.g. signer-set contracts, are commonly predictable/public on-chain) can call `GET /v2/stackerdb/<addr>/<contract>/replicas` and, in the window where `public_ip_address` is unset, receive the node's raw private/internal bind address in the JSON body via `HttpResponseContents::try_from_json(&naddrs)` — even when the operator explicitly configured `private_neighbors=false` (`allow_private=false`) specifically to prevent such disclosure. This is a scoped information-disclosure bug: it discloses network topology info about the node operator's internal network to an unauthenticated party, repeatable per request.

### Likelihood Explanation
Preconditions: (1) the node hosts at least one StackerDB whose contract identifier the attacker knows, (2) `local_peer.public_ip_address` is `None` at request time (true at startup, for nodes behind NAT before a peer replies to a NAT-punch request, or for nodes bound to a private/loopback address that never resolves a distinct public IP). No secret, peer key, or privileged role is required — only a plain HTTP GET to the node's RPC port, remotely reachable. Cost is a single HTTP request, repeatable at will.

### Recommendation
Apply the same `allow_private`/`is_anynet` filter to the local-peer entry before inserting it, e.g. only `naddrs.insert(0, local_peer.to_public_neighbor_addr())` if the resulting address passes `!addr.is_anynet() && (allow_private || !addr.is_in_private_range())`, mirroring the filter used for the `PeerDB` results.

### Proof of Concept
Rust test in `stackslib::net::api::liststackerdbreplicas`:
1. Construct a `LocalPeer` with `addrbytes` set to a private-range address (e.g. `10.0.0.5`), `public_ip_address = None`, and `stacker_dbs = vec![test_contract_id.clone()]`.
2. Set connection options so `private_neighbors = false` (`allow_private = false`).
3. Populate `PeerDB` with zero or more replicas (any, unrelated to the assertion).
4. Send `GET /v2/stackerdb/<addr>/<contract>/replicas` through `RPCListStackerDBReplicasRequestHandler::try_handle_request`.
5. Decode the JSON body into `Vec<NeighborAddress>` and assert it does NOT contain an entry whose `addrbytes` equals the private `10.0.0.5` address — the assertion should fail against current code because `naddrs.insert(0, local_peer.to_public_neighbor_addr())` at line 153 inserts it unconditionally, exposing the private address despite `allow_private == false`.

### Citations

**File:** stackslib/src/net/api/liststackerdbreplicas.rs (L36-45)
```rust
pub struct RPCListStackerDBReplicasRequestHandler {
    pub contract_identifier: Option<QualifiedContractIdentifier>,
}

impl RPCListStackerDBReplicasRequestHandler {
    pub fn new() -> Self {
        Self {
            contract_identifier: None,
        }
    }
```

**File:** stackslib/src/net/api/liststackerdbreplicas.rs (L68-85)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        _body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() != 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected 0-length body".to_string(),
            ));
        }

        let contract_identifier = request::get_contract_address(captures, "address", "contract")?;
        self.contract_identifier = Some(contract_identifier);

        Ok(HttpRequestContents::new().query_string(query))
    }
```

**File:** stackslib/src/net/api/liststackerdbreplicas.rs (L126-145)
```rust
        let mut naddrs = match replicas_resp {
            Ok(neighbors) => neighbors
                .into_iter()
                .map(|neighbor| NeighborAddress::from_neighbor(&neighbor))
                .filter(|naddr| {
                    if naddr.addrbytes.is_anynet() {
                        // don't expose 0.0.0.0 or ::1
                        return false;
                    }
                    if !allow_private && naddr.addrbytes.is_in_private_range() {
                        // filter unroutable network addresses
                        return false;
                    }
                    true
                })
                .collect::<Vec<_>>(),
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };
```

**File:** stackslib/src/net/api/liststackerdbreplicas.rs (L147-154)
```rust
        if local_peer
            .stacker_dbs
            .iter()
            .find(|contract_id| contract_id == &&contract_identifier)
            .is_some()
        {
            naddrs.insert(0, local_peer.to_public_neighbor_addr());
        }
```

**File:** stackslib/src/net/db.rs (L160-183)
```rust
    pub fn to_neighbor_addr(&self) -> NeighborAddress {
        NeighborAddress {
            addrbytes: self.addrbytes.clone(),
            port: self.port,
            public_key_hash: Hash160::from_node_public_key(&StacksPublicKey::from_private(
                &self.private_key,
            )),
        }
    }

    /// Best-effort attempt to calculate a publicly-routable neighbor address for local peer
    pub fn to_public_neighbor_addr(&self) -> NeighborAddress {
        if let Some((peer_addr, peer_port)) = self.public_ip_address.as_ref() {
            NeighborAddress {
                addrbytes: peer_addr.clone(),
                port: *peer_port,
                public_key_hash: Hash160::from_node_public_key(&StacksPublicKey::from_private(
                    &self.private_key,
                )),
            }
        } else {
            self.to_neighbor_addr()
        }
    }
```

**File:** stackslib/src/net/p2p.rs (L3081-3087)
```rust
                        self.public_ip_learned_at = get_epoch_time_secs();
                        self.public_ip_retries = 0;

                        // if our IP address changed, then disconnect witih everyone
                        let old_ip = self.local_peer.public_ip_address.clone();
                        self.local_peer.public_ip_address =
                            Some((data.addrbytes, self.bind_nk.port));
```

**File:** stackslib/src/net/p2p.rs (L3168-3179)
```rust
    /// Reset all state for querying our public IP address
    fn public_ip_reset(&mut self) {
        debug!("{:?}: reset public IP query state", &self.local_peer);

        self.public_ip_reply_handle = None;
        self.public_ip_confirmed = false;

        if self.public_ip_learned {
            // will go relearn it if it wasn't given
            self.local_peer.public_ip_address = None;
        }
    }
```
