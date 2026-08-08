### Title
Unstaked/staked peer connection cap keyed by free, self-signed pubkey allows a single source IP to monopolize the entire unstaked/staked connection pool - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ConnectionTableKey::new` keys a connection-table entry by `Pubkey` whenever the client presents one, and `try_add_connection` enforces `max_connections_per_(un)staked_peer` per-key rather than per-source-IP. Because a TPU client's "pubkey" is nothing more than a self-signed TLS certificate (`get_remote_pubkey`/`get_pubkey_from_tls_certificate`), an unprivileged attacker can generate a fresh `Keypair` for every QUIC handshake at essentially zero cost, so the per-peer cap that is supposed to stop one source from hogging the pool is trivially bypassed from a single IP.

### Finding Description
`ConnectionTableKey::new` chooses the table key based on whether a pubkey was recovered from the peer's TLS identity: [1](#0-0) 

`ConnectionTable::try_add_connection` then enforces `max_connections_per_peer` only against the vector stored under that single key: [2](#0-1) 

Both QoS implementations (`SwQos::cache_new_connection` and `SimpleQos::cache_new_connection`) build the key from `remote_addr.ip()` and `conn_context.remote_pubkey`, defaulting to the pubkey path whenever one is present: [3](#0-2) [4](#0-3) 

The "pubkey" is recovered from the client's self-signed certificate with no proof of stake, registration, or expense — any client can generate a new `Keypair` and derive a new cert via `new_dummy_x509_certificate`, and the server accepts it as long as the TLS handshake signature is valid (`SkipClientVerification` only checks cryptographic validity of the signature, not identity legitimacy): [5](#0-4) [6](#0-5) 

Existing unit tests confirm the intended (but exploitable) behavior explicitly: `test_prune_table_with_unique_pubkeys` shows that using a unique pubkey per connection lets a client add far more entries than `max_connections_per_peer` would allow if keyed by IP: [7](#0-6) 

By contrast, `test_prune_table_with_non_unique_pubkeys` shows the cap only kicks in when the *same* pubkey is reused: [8](#0-7) 

The only remaining backstop is the global table size cap (`max_unstaked_connections`/`max_staked_connections`), which triggers LRU/random pruning once the whole table (across all peers/IPs) is full: [9](#0-8) [10](#0-9) 

This global cap does bound total memory/connection consumption cluster-wide, but it does **not** protect against one physical source IP claiming a disproportionate share of that global pool: since the per-peer key is decoupled from the IP, `max_connections_per_unstaked_peer` (designed as a fairness knob to stop a single source from starving others) becomes meaningless against an attacker that rotates identities. A single IP can occupy connection slots up to `max_unstaked_connections` (the whole unstaked pool), rather than being capped at `max_connections_per_unstaked_peer`, at the cost of nothing but generating new keypairs (cheap Ed25519 keygen).

### Impact Explanation
This falls under the QoS/connection-limit-evasion bounty category: a single unstaked, unprivileged, unstaked source can capture up to the entire unstaked (or staked, if it can generate stake-eligible-looking connections is not applicable, but definitely unstaked) `ConnectionTable` capacity by rotating self-signed pubkeys per handshake, starving all other unstaked clients/IPs of TPU connection slots and stream capacity. This is a real QoS-fairness bypass (per-peer cap intended to enforce fairness across sources is fully defeated), even though the absolute global connection ceiling (`max_unstaked_connections`) still bounds memory. The practical effect is a low-cost denial-of-service against legitimate unstaked senders trying to reach the leader's TPU.

### Likelihood Explanation
Preconditions are trivially met: no stake, no special config, no leaked keys — just an unprivileged remote client able to open QUIC connections to the public TPU port and present a fresh, valid (but arbitrary) self-signed certificate per connection. Generating a new Ed25519 keypair and cert per handshake is computationally negligible, so the attack is easily automatable and repeatable at scale (limited only by handshake rate / global table pruning cadence).

### Recommendation
For the unstaked path (and arguably staked-unverified path), key the per-peer connection cap on source IP (or a combination that cannot be freely rotated by the client) rather than on a self-asserted pubkey, or additionally track a secondary IP-based cap that is enforced together with (not instead of) the pubkey-based cap. At minimum, cap `(IP, count)` in addition to `(Pubkey, count)` so no single IP can exceed `max_connections_per_unstaked_peer` (or a distinct `max_connections_per_ip`) regardless of how many distinct pubkeys it presents.

### Proof of Concept
```rust
// streamer/src/nonblocking/quic.rs (test module)
#[test]
fn test_single_ip_bypasses_per_peer_cap_via_pubkey_rotation() {
    agave_logger::setup();
    let cancel = CancellationToken::new();
    let mut table = ConnectionTable::new(ConnectionTableType::Unstaked, cancel);
    let stats = Arc::new(StreamerStats::default());

    let max_connections_per_peer = 10;
    let attacker_ip = IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 1));

    // Attacker opens far more than max_connections_per_peer connections,
    // each with a fresh Pubkey, all from the SAME source IP.
    let num_attempts = max_connections_per_peer * 5;
    let mut accepted = 0;
    for i in 0..num_attempts {
        let fresh_pubkey = Pubkey::new_unique(); // free, unstaked identity
        let key = ConnectionTableKey::new(attacker_ip, Some(fresh_pubkey));
        if table
            .try_add_connection(
                key,
                0,
                ClientConnectionTracker::new(stats.clone(), 10_000).unwrap(),
                None,
                ConnectionPeerType::Unstaked,
                Arc::new(AtomicU64::new(i as u64)),
                max_connections_per_peer,
                || Arc::new(NullStreamerCounter {}),
            )
            .is_some()
        {
            accepted += 1;
        }
    }

    // BUG: an invariant tying the cap to source IP should have limited
    // `accepted` to max_connections_per_peer regardless of pubkey rotation.
    // Instead, every attempt succeeds because each gets its own table bucket.
    assert!(
        accepted > max_connections_per_peer,
        "expected cap bypass to be demonstrated, got {accepted} accepted \
         connections from a single IP (cap was {max_connections_per_peer})"
    );
}
```
Expected result on current code: `accepted == num_attempts` (all 50 connections succeed from one IP), demonstrating the per-peer cap provides no protection when keyed by a freely rotatable pubkey. A fixed implementation should additionally cap accepted connections per source IP to `max_connections_per_peer` (or a dedicated per-IP limit), causing this assertion to fail (i.e., `accepted <= max_connections_per_peer`) once remediated.

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

**File:** streamer/src/nonblocking/quic.rs (L1008-1024)
```rust
    pub(crate) fn try_add_connection<F: FnOnce() -> Arc<S>>(
        &mut self,
        key: ConnectionTableKey,
        port: u16,
        client_connection_tracker: ClientConnectionTracker,
        connection: Option<Connection>,
        peer_type: ConnectionPeerType,
        last_update: Arc<AtomicU64>,
        max_connections_per_peer: usize,
        stream_counter_factory: F,
    ) -> Option<(Arc<AtomicU64>, CancellationToken, Arc<S>)> {
        let connection_entry = self.table.entry(key).or_default();
        let has_connection_capacity = connection_entry
            .len()
            .checked_add(1)
            .map(|c| c <= max_connections_per_peer)
            .unwrap_or(false);
```

**File:** streamer/src/nonblocking/quic.rs (L1732-1770)
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

        let new_size = 3;
        let pruned = table.prune_oldest(new_size);
        assert_eq!(pruned, num_entries as usize - new_size);
        assert_eq!(table.table.len(), new_size);
        assert_eq!(table.total_size, new_size);
        for pubkey in pubkeys.iter().take(num_entries as usize).skip(new_size - 1) {
            table.remove_connection(ConnectionTableKey::Pubkey(*pubkey), 0, 0);
        }
        assert_eq!(table.total_size, 0);
        assert_eq!(stats.open_connections.load(Ordering::Relaxed), 0);
    }
```

**File:** streamer/src/nonblocking/quic.rs (L1821-1861)
```rust
    #[test]
    fn test_prune_table_with_non_unique_pubkeys() {
        agave_logger::setup();
        let cancel = CancellationToken::new();
        let mut table = ConnectionTable::new(ConnectionTableType::Unstaked, cancel);

        let max_connections_per_peer = 10;
        let pubkey = Pubkey::new_unique();
        let stats: Arc<StreamerStats> = Arc::new(StreamerStats::default());

        (0..max_connections_per_peer).for_each(|i| {
            table
                .try_add_connection(
                    ConnectionTableKey::Pubkey(pubkey),
                    0,
                    ClientConnectionTracker::new(stats.clone(), 1000).unwrap(),
                    None,
                    ConnectionPeerType::Unstaked,
                    Arc::new(AtomicU64::new(i as u64)),
                    max_connections_per_peer,
                    || Arc::new(NullStreamerCounter {}),
                )
                .unwrap();
        });

        // We should NOT be able to add more entries than max_connections_per_peer, since we are
        // using the same peer pubkey.
        assert!(
            table
                .try_add_connection(
                    ConnectionTableKey::Pubkey(pubkey),
                    0,
                    ClientConnectionTracker::new(stats.clone(), 1000).unwrap(),
                    None,
                    ConnectionPeerType::Unstaked,
                    Arc::new(AtomicU64::new(10)),
                    max_connections_per_peer,
                    || Arc::new(NullStreamerCounter {})
                )
                .is_none()
        );
```

**File:** streamer/src/nonblocking/swqos.rs (L205-219)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L241-256)
```rust
    fn prune_unstaked_connection_table(
        &self,
        unstaked_connection_table: &mut ConnectionTable<ConnectionStreamCounter>,
        max_unstaked_connections: usize,
        stats: Arc<StreamerStats>,
    ) {
        if unstaked_connection_table.total_size >= max_unstaked_connections {
            // Prune the connection table down to 90% capacity
            const PRUNE_TABLE_RATIO: f64 = 0.90;
            let max_connections = (PRUNE_TABLE_RATIO * (max_unstaked_connections as f64)) as usize;
            let num_pruned = unstaked_connection_table.prune_oldest(max_connections);
            stats
                .num_evictions_unstaked
                .fetch_add(num_pruned, Ordering::Relaxed);
        }
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L357-365)
```rust

                    if connection_table_l.total_size >= self.config.max_staked_connections {
                        let num_pruned =
                            connection_table_l.prune_random(PRUNE_RANDOM_SAMPLE_SIZE, stake);
                        self.stats
                            .num_evictions_staked
                            .fetch_add(num_pruned, Ordering::Relaxed);
                        update_open_connections_stat(&self.stats, &connection_table_l);
                    }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L206-223)
```rust
        let key = ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey);
        if let Some((last_update, cancel_connection, stream_counter)) = connection_table_l
            .try_add_connection(
                key,
                remote_addr.port(),
                client_connection_tracker,
                Some(connection.clone()),
                conn_context.peer_type(),
                conn_context.last_update.clone(),
                self.config.max_connections_per_peer,
                || {
                    Arc::new(TokenBucket::new(
                        self.config.max_streams_per_second,
                        self.config.max_streams_per_second,
                        self.config.max_streams_per_second as f64,
                    ))
                },
            )
```

**File:** tls-utils/src/tls_certificates.rs (L120-144)
```rust
pub fn get_pubkey_from_tls_certificate(
    der_cert: &rustls::pki_types::CertificateDer,
) -> Option<Pubkey> {
    let (_, cert) = X509Certificate::from_der(der_cert.as_ref()).ok()?;
    match cert.public_key().parsed().ok()? {
        PublicKey::Unknown(key) => Pubkey::try_from(key).ok(),
        _ => None,
    }
}

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

**File:** tls-utils/src/skip_client_verification.rs (L23-76)
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

    fn root_hint_subjects(&self) -> &[DistinguishedName] {
        &[]
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }

    fn offer_client_auth(&self) -> bool {
        true
    }

    fn client_auth_mandatory(&self) -> bool {
        self.offer_client_auth()
    }
}
```
