### Title
Unstaked TPU clients can bypass per-IP connection quotas via unlimited self-signed pubkeys, causing Sybil-style QoS evasion - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ConnectionTableKey::new` keys a connection by `Pubkey` whenever a client presents any pubkey during the QUIC/TLS handshake, and both `SwQos::cache_new_connection` (streamer/src/nonblocking/swqos.rs) and `SimpleQos::cache_new_connection` (streamer/src/nonblocking/simple_qos.rs) pass `conn_context.remote_pubkey` unconditionally into this key builder — including for `ConnectionPeerType::Unstaked` connections. Because `get_remote_pubkey`/`get_pubkey_from_tls_certificate` (tls-utils/src/tls_certificates.rs) accept any self-signed client certificate with no relation to actual stake, an unstaked attacker on a single source IP can mint an unlimited number of distinct Ed25519 keypairs and thereby obtain a fresh `ConnectionTableKey::Pubkey(...)` bucket per connection, each independently allowed up to `max_connections_per_unstaked_peer` connections, instead of being aggregated under a single `ConnectionTableKey::IP(...)` bucket.

### Finding Description
- `ConnectionTableKey::new(ip, maybe_pubkey)` returns `ConnectionTableKey::Pubkey(pubkey)` whenever `maybe_pubkey` is `Some`, and only falls back to `ConnectionTableKey::IP(ip)` when no pubkey was extracted: [1](#0-0) 
- `get_remote_pubkey` recovers a Solana pubkey from any single self-signed leaf certificate presented during the QUIC/TLS handshake, without checking whether that pubkey corresponds to a staked identity: [2](#0-1) 
- `SwQos::cache_new_connection` builds the connection-table key from `conn_context.remote_pubkey` regardless of whether `conn_context.peer_type()` is `Staked` or `Unstaked`, and looks up `max_connections_per_peer` from `max_connections_per_unstaked_peer` in the unstaked case: [3](#0-2) 
- The codebase's own unit test explicitly documents and validates this behavior for `ConnectionPeerType::Unstaked`: adding 15 connections each keyed by a unique `Pubkey` all succeed even though `max_connections_per_peer` is 10, because "each entry is from a different peer pubkey": [4](#0-3) 

Since TLS client-certificate verification is disabled (`SkipClientVerification` accepts any client certificate [5](#0-4) ) and generating an Ed25519 keypair is free, an attacker on one physical machine/IP can open many QUIC connections, each presenting a distinct throwaway keypair as its client certificate. Every such connection resolves to a distinct `ConnectionTableKey::Pubkey`, so the per-peer connection cap (`max_connections_per_unstaked_peer`) is enforced per fabricated pubkey rather than per source IP, letting a single IP claim an arbitrarily larger share of the shared unstaked connection budget than a normal single-key peer would be allowed.

### Impact Explanation
This falls under the described bounty scope: "An unprivileged, unstaked client can bypass or unfairly capture connection, stream, or per-IP QoS limits and starve legitimate senders of TPU capacity." By rotating self-signed pubkeys from one IP, the attacker multiplies its effective per-peer connection quota (`DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER` per distinct fabricated key) well beyond what the per-IP aggregation (`ConnectionTableKey::IP`) is meant to enforce, letting one machine consume a disproportionate fraction of `max_unstaked_connections` and starve other legitimate unstaked senders of TPU ingress capacity.

### Likelihood Explanation
Fully reachable by an unprivileged remote unstaked client requiring only: opening ordinary QUIC connections to the leader's public TPU port with a distinct self-signed keypair per connection (no stake, no gossip/peer trust, no operator misconfiguration needed). Ed25519 keypair generation and TLS handshake completion are cheap and require no coordination, making this trivially repeatable at scale from a single source IP.

### Recommendation
For `ConnectionPeerType::Unstaked` connections, force aggregation by source IP regardless of whether a client presented a pubkey (i.e., pass `None` for `maybe_pubkey` to `ConnectionTableKey::new` unless the peer's pubkey is confirmed to have nonzero stake), so unstaked peers cannot escape per-IP quota enforcement by rotating self-signed identities.

### Proof of Concept
Add a test to `streamer/src/nonblocking/quic.rs` (or `swqos.rs`) that reproduces the scenario end-to-end via `SwQos::cache_new_connection`/`ConnectionTable::try_add_connection` from a single IP:

```rust
#[test]
fn test_unstaked_pubkey_rotation_bypasses_per_ip_cap() {
    let cancel = CancellationToken::new();
    let mut table = ConnectionTable::new(ConnectionTableType::Unstaked, cancel);
    let stats = Arc::new(StreamerStats::default());
    let max_connections_per_peer = 8;
    let attacker_ip = IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1));

    // Simulate a single physical machine rotating self-signed pubkeys.
    let num_fake_identities = 50;
    let mut total_added = 0;
    for i in 0..num_fake_identities {
        let fake_pubkey = Pubkey::new_unique(); // free, no stake required
        let key = ConnectionTableKey::new(attacker_ip, Some(fake_pubkey));
        if table.try_add_connection(
            key, 0,
            ClientConnectionTracker::new(stats.clone(), 10_000).unwrap(),
            None,
            ConnectionPeerType::Unstaked,
            Arc::new(AtomicU64::new(i as u64)),
            max_connections_per_peer,
            || Arc::new(NullStreamerCounter {}),
        ).is_some() {
            total_added += 1;
        }
    }

    // Expected (buggy) result: attacker gets far more than max_connections_per_peer
    // from a single IP because each pubkey is a distinct bucket.
    assert!(total_added > max_connections_per_peer,
        "single IP obtained {total_added} connections, exceeding per-IP cap of {max_connections_per_peer}");
}
```
Expected assertion failure/success demonstrates that aggregate capacity from one IP is bounded by number of fabricated pubkeys, not by IP, confirming the QoS bypass.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L922-928)
```rust
impl ConnectionTableKey {
    pub(crate) fn new(ip: IpAddr, maybe_pubkey: Option<Pubkey>) -> Self {
        maybe_pubkey.map_or(ConnectionTableKey::IP(ip), |pubkey| {
            ConnectionTableKey::Pubkey(pubkey)
        })
    }
}
```

**File:** streamer/src/nonblocking/quic.rs (L1732-1758)
```rust
    #[test]
    fn test_prune_table_with_unique_pubkeys() {
        agave_logger::setup();
        let cancel = CancellationToken::new();
        let mut table = ConnectionTable::new(ConnectionTableType::Unstaked, cancel);

        // We should be able to add more entries than max_connections_per_peer, since each entry is
        // from a different peer pubkey.
        let num_entries = 15;
        let max_connections_per_peer = 10;
        let stats = Arc::new(StreamerStats::default());

        let pubkeys: Vec<_> = (0..num_entries).map(|_| Pubkey::new_unique()).collect();
        for (i, pubkey) in pubkeys.iter().enumerate() {
            table
                .try_add_connection(
                    ConnectionTableKey::Pubkey(*pubkey),
                    0,
                    ClientConnectionTracker::new(stats.clone(), 1000).unwrap(),
                    None,
                    ConnectionPeerType::Unstaked,
                    Arc::new(AtomicU64::new(i as u64)),
                    max_connections_per_peer,
                    || Arc::new(NullStreamerCounter {}),
                )
                .unwrap();
        }
```

**File:** tls-utils/src/tls_certificates.rs (L130-144)
```rust
/// Recover the peer's Solana pubkey from a `quinn::Connection`. Accepts
/// only a self-signed cert chain of length 1 (matches the cert shape
/// produced by [`new_dummy_x509_certificate`]).
///
/// Single source of truth for anything doing QUIC and using
/// Solana validator identities for auth.
pub fn get_remote_pubkey(connection: &quinn::Connection) -> Option<Pubkey> {
    connection
        .peer_identity()?
        .downcast::<Vec<rustls::pki_types::CertificateDer>>()
        .ok()
        .filter(|certs| certs.len() == 1)?
        .first()
        .and_then(get_pubkey_from_tls_certificate)
}
```

**File:** streamer/src/nonblocking/swqos.rs (L203-219)
```rust
        let remote_addr = conn_context.remote_address;

        let max_connections_per_peer = match conn_context.peer_type() {
            ConnectionPeerType::Unstaked => self.config.max_connections_per_unstaked_peer,
            ConnectionPeerType::Staked(_) => self.config.max_connections_per_staked_peer,
        };
        if let Some((last_update, cancel_connection, stream_counter)) = connection_table_l
            .try_add_connection(
                ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey),
                remote_addr.port(),
                client_connection_tracker,
                Some(connection.clone()),
                conn_context.peer_type(),
                conn_context.last_update.clone(),
                max_connections_per_peer,
                || Arc::new(ConnectionStreamCounter::new()),
            )
```

**File:** tls-utils/src/skip_client_verification.rs (L23-31)
```rust
impl ClientCertVerifier for SkipClientVerification {
    fn verify_client_cert(
        &self,
        _end_entity: &CertificateDer,
        _intermediates: &[CertificateDer],
        _now: UnixTime,
    ) -> Result<ClientCertVerified, Error> {
        Ok(ClientCertVerified::assertion())
    }
```
