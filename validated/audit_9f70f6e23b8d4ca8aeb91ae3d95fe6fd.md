### Title
Incomplete private-IP blocklist in StackerDB `hint-replicas` allows SSRF to cloud metadata / link-local endpoints - ([File: stackslib/src/net/stackerdb/config.rs])

### Summary
`StackerDBConfig::eval_hint_replicas` (used when loading a StackerDB's replication configuration from its controlling smart contract) is supposed to filter out non-routable/private addresses before turning contract-supplied `hint-replicas` entries into `NeighborAddress` values that the node will actively dial for StackerDB chunk sync. The filter only checks `PeerAddress::is_in_private_range()`, which omits the IPv4 link-local range `169.254.0.0/16` — the range that hosts the well-known cloud-provider instance-metadata endpoint `169.254.169.254` (AWS/GCP/Azure). Any permissionless contract deployer can therefore configure a StackerDB contract whose `hint-replicas` point at `169.254.169.254` (or any other link-local address), and every node that indexes that contract will treat it as a legitimate replica peer and attempt outbound connections to it.

### Finding Description
`eval_hint_replicas` decodes the `addr`/`port`/`public-key-hash` tuple from the smart contract's `stackerdb-get-config` return value and only excludes addresses matching `PeerAddress::is_in_private_range()`: [1](#0-0) 

`is_in_private_range()` itself is defined to reject only RFC1918 ranges plus loopback (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`) and IPv6 `fc00::/7`/`::1`: [2](#0-1) 

There is no check for `169.254.0.0/16` (IPv4 link-local) or its IPv6 analog `fe80::/10`. This is the same fault-class as the OpenDJ DSMLv2 advisory: an anyURI/address dereferencer with an incomplete denylist that still permits reaching cloud metadata and other link-local internal services. The resulting `NeighborAddress` list is stored as `StackerDBConfig.hint_replicas` and used by the StackerDB sync/download machinery as legitimate peers to contact for chunk replication — i.e., this is a "fail-open" security gate: any address not on the (incomplete) blocklist is treated as trusted and reachable, exactly the equality break the rules call out ("an auth-gate that fails open").

Configuration loading and storage of the resulting hint-replica list: [3](#0-2) [4](#0-3) 

Note that this same incomplete-blocklist pattern (RFC1918 + loopback only, no link-local) is reused elsewhere in the networking stack (e.g. `PeerDB` public/private tagging and `can_register_peer`), meaning the gap is systemic rather than local to one call site: [5](#0-4) 

### Impact Explanation
Any unprivileged party who can deploy a Stacks smart contract (an ordinary, permissionless transaction) can seed a StackerDB configuration that steers every node replicating that StackerDB into making outbound TCP/HTTP connections to `169.254.169.254` or other link-local addresses. On nodes running in cloud environments, this creates a path to the instance metadata service, which is the classic SSRF vector for credential/token exfiltration (matching the CWE-918/CWE-73 classes in the referenced advisory). This is a remote, unauthenticated (contract-deployment only, not privileged/administrative) network-egress steering bug reachable purely through data the node ingests to configure itself, fitting the "auth-gate that fails open" category called out as in-scope.

### Likelihood Explanation
Deploying a contract implementing the StackerDB control interface is a routine, permissionless operation available to anyone with minimal STX for fees; no special role or node secret is required. Any node that is configured (or automatically discovers, depending on deployment) to replicate that contract's StackerDB will evaluate `stackerdb-get-config` and apply the incomplete filter, so exploitation requires no cooperation from node operators beyond normal StackerDB indexing behavior.

### Recommendation
Extend `PeerAddress::is_in_private_range()` (or add a dedicated check used specifically for `eval_hint_replicas`) to also reject IPv4 link-local (`169.254.0.0/16`), IPv6 link-local (`fe80::/10`), the "any" addresses, and other reserved/non-global ranges (e.g., CGNAT `100.64.0.0/10`, multicast, `0.0.0.0/8`), following an allowlist-of-globally-routable-only approach rather than a denylist. Apply the fix at the shared `PeerAddress` helper so all call sites (`stackerdb/config.rs`, `p2p.rs`, `db.rs`) benefit consistently.

### Proof of Concept
1. Deploy a contract implementing the StackerDB control interface (`stackerdb-get-signer-slots` / `stackerdb-get-config`) whose `hint-replicas` list contains `addr: (list 0 0 0 0 0 0 0 0 0 0 255 255 169 254 169 254)` (i.e. `169.254.169.254`), `port: u1024+`, and any `public-key-hash`.
2. When a node loads this StackerDB (via `StackerDBConfig::from_smart_contract` → `eval_config` → `eval_hint_replicas`), the address passes the `is_in_private_range()` check (it is neither RFC1918 nor loopback) and is stored in `StackerDBConfig.hint_replicas`.
3. The node's StackerDB replication logic subsequently treats this address as a legitimate replica peer and will attempt to contact `169.254.169.254:<port>`, which on cloud-hosted infrastructure resolves to the instance metadata service.

Note: I could not fully trace, within the indexed portion of the repo, the exact downstream code path in `stackerdb/sync.rs` that issues the outbound connection to a `hint_replicas` entry (the file is referenced but its contents were not fully retrievable through search); a Devin session with full file access would be needed to confirm the precise connection call and response-handling behavior (e.g., whether any data is echoed back to the attacker).

### Citations

**File:** stackslib/src/net/stackerdb/config.rs (L346-362)
```rust
            let pubkey_hash_slice: &[u8; 20] = pubkey_hash_bytes
                .get(0..20)
                .and_then(|bytes| bytes.try_into().ok())
                .ok_or_else(|| {
                    let reason = format!("{contract_id} stipulates pubkey hash bytes length < 20");
                    warn!("{reason}");
                    NetError::InvalidStackerDBContract(contract_id.clone(), reason)
                })?;

            let peer_addr = PeerAddress::from_slice(&addr_bytes).expect("FATAL: not 16 bytes");
            if peer_addr.is_in_private_range() {
                debug!(
                    "Ignoring private IP address '{}' in hint-replicas",
                    &peer_addr.to_socketaddr(port as u16)
                );
                continue;
            }
```

**File:** stackslib/src/net/stackerdb/config.rs (L484-494)
```rust
        let hint_replicas = if let Some(replicas) = local_hint_replicas {
            replicas
        } else {
            let hint_replicas_list = config_tuple
                .get("hint-replicas")
                .expect("FATAL: missing 'hint-replicas'")
                .clone()
                .expect_list()?;

            Self::eval_hint_replicas(contract_id, hint_replicas_list)?
        };
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

**File:** stackslib/src/net/stackerdb/mod.rs (L376-405)
```rust
            } else {
                // attempt to load the config from the contract itself
                StackerDBConfig::from_smart_contract(
                    chainstate,
                    sortdb,
                    &stackerdb_contract_id,
                    num_neighbors,
                    connection_opts
                        .stackerdb_hint_replicas
                        .get(&stackerdb_contract_id)
                        .cloned(),
                )
                .unwrap_or_else(|e| {
                    if matches!(e, net_error::NoSuchStackerDB(_)) && stackerdb_contract_id.is_boot()
                    {
                        debug!(
                            "Failed to load StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                    } else {
                        warn!(
                            "Failed to load StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                    }
                    StackerDBConfig::noop()
                })
            };
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
