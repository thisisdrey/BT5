### Title
Unbounded `modified_slots` array in `/stackerdb_chunks` JSON body forces unbounded `secp256k1` `recover_pk` calls before any signature check - ([File: libsigner/src/events.rs])

### Summary
`process_event` deserializes the entire attacker-supplied JSON body into `E` (e.g. `StackerDBChunksEvent`) before any per-chunk authentication runs, and neither `process_event` nor the `TryFrom<StackerDBChunksEvent>` implementation caps the number of elements in `modified_slots`. Because the signer's event-receiver HTTP endpoint is documented to be bindable on `0.0.0.0` and has no authentication of its own, any remote party that can reach that port can submit one oversized POST that forces the signer to run `chunk.recover_pk()` (an ECDSA public-key recovery) once per array element, with no per-request cap.

### Finding Description
`process_event` in `libsigner/src/events.rs` reads the full HTTP body, JSON-decodes it into `E: DeserializeOwned` via `serde_json::from_slice`, and only afterward calls `json_event.try_into()` which performs the type-specific checks: [1](#0-0) 

For the `signers-N-M` contract branch, `TryFrom<StackerDBChunksEvent>` iterates `event.modified_slots` with `.filter_map(...)`, and for every element that passes the cheap payload-type-byte check it calls `chunk.recover_pk()` (secp256k1 signature recovery) before deserializing the payload: [2](#0-1) 

There is no cap anywhere in this path on `Vec<StackerDBChunkData>` length; each element's `data` field is only bounded per-element (not in aggregate) by whatever size checks exist on `StackerDBChunkData`. The number of `recover_pk` invocations per POST is therefore proportional to N, the number of JSON array entries the attacker includes, with no upper bound enforced by `events.rs`.

Critically, this endpoint is not merely an internal node→signer relay guarded by network topology: the shipped reference configuration explicitly documents binding it to all interfaces (`endpoint = "0.0.0.0:30000"`), and `stacks-signer/src/lib.rs` emits a runtime warning acknowledging that no additional authentication is enforced on this HTTP listener beyond the assumption that only a trusted local node will contact it: [3](#0-2) 

This means the "authentication" the audit question worries about (per-chunk signature checks inside `TryFrom`) is the *only* gate on this endpoint — there is no request-level auth token, no TLS client cert, and no HMAC on the POST body itself. A remote unprivileged party that can route packets to the bound port can submit an arbitrary JSON body directly, bypassing the actual stacks-node entirely.

### Impact Explanation
An attacker who can reach the signer's bound event-receiver port can send a single crafted `StackerDBChunksEvent` JSON body containing many thousands of `modified_slots` entries (each with a minimal but valid-looking `data` field starting with a recognized `SignerMessageTypePrefix` byte) targeting a `signers-N-M` contract. This forces the signer process to perform thousands of `secp256k1` public-key-recovery operations synchronously inside the event-receiver thread's `next_event()` call, consuming CPU and delaying/blocking the signer's HTTP receiver from servicing legitimate events from the real node (since `main_loop` processes requests serially). This is a bounded-but-amplified compute-DoS on the signer's event-ingestion path — matching the "bounded compute DoS" category, scoped to the signer process rather than the full stacks-node.

### Likelihood Explanation
Preconditions: the signer's event-receiver endpoint must be reachable by the attacker (the maintained sample config documents binding to `0.0.0.0`, and the code enforces no additional network-layer authentication check on this listener). Attacker cost is a single crafted HTTP POST with a large but not enormous JSON body (element count in the thousands, not requiring huge total bytes since only the array cardinality drives the `recover_pk` cost). This is fully repeatable — the attacker can resend the same or larger payload indefinitely, and each request independently triggers the O(N) `recover_pk` cost before the signature-recovery check can reject any of the entries.

### Recommendation
Cap the number of `modified_slots` entries accepted per `StackerDBChunksEvent` (and/or the total decoded JSON size) before iterating and calling `recover_pk`, mirroring the existing per-chunk `STACKERDB_MAX_CHUNK_SIZE` cap. Additionally, bind the signer's event-receiver by default to `127.0.0.1` (or require a shared secret/HMAC on the POST body) so that the endpoint is not reachable by arbitrary remote parties, consistent with the trust model that only the paired stacks-node should be able to post events. Enforce the array-length cap in `process_event` (or immediately in `TryFrom<StackerDBChunksEvent>`) prior to entering the `.filter_map` loop, rejecting oversized arrays with `EventError::MalformedRequest` before any `recover_pk` call.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) - PoC sketch
#[test]
fn poc_unbounded_modified_slots_forces_many_recover_pk_calls() {
    use std::time::Instant;
    // Build a StackerDBChunksEvent JSON body for a `signers-0-1` contract with
    // N (e.g. 20_000) modified_slots entries, each containing a minimal
    // StacksMessageCodec-prefixed payload byte (matching SignerMessageTypePrefix)
    // followed by a syntactically-valid-looking (but bogus) ECDSA recoverable
    // signature field, so that `chunk.recover_pk()` is attempted for every entry.
    let body = build_stackerdb_chunks_event_json(/* contract = */ "signers-0-1", /* n = */ 20_000);

    let start = Instant::now();
    // Feed `body` directly to a bound SignerEventReceiver via a raw TCP POST
    // to /stackerdb_chunks, exactly as process_event::<T, StackerDBChunksEvent> consumes it.
    post_to_signer_event_receiver("/stackerdb_chunks", &body);
    let elapsed = start.elapsed();

    // Assert that CPU time scales linearly with N (compute amplification),
    // and that no cap rejected the array before recover_pk was invoked N times.
    assert!(elapsed.as_millis() > EXPECTED_SINGLE_CHUNK_BASELINE_MS * 1000);
}
```
Note: exact byte-level crafting of `StackerDBChunkData` (`libstackerdb/src/libstackerdb.rs`) fields to reach the `recover_pk` call for each of N entries was not fully traced in this session (index does not show the complete `StackerDBChunkData`/`recover_pk` implementation); a background Devin session with full repo access should verify the precise per-chunk field layout and any existing per-element size guard before finalizing the PoC bytes.

### Citations

**File:** libsigner/src/events.rs (L519-542)
```rust
fn process_event<T, E>(mut request: HttpRequest) -> Result<SignerEvent<T>, EventError>
where
    T: SignerEventTrait,
    E: serde::de::DeserializeOwned + TryInto<SignerEvent<T>, Error = EventError>,
{
    let mut body = String::new();

    if let Err(e) = request.as_reader().read_to_string(&mut body) {
        error!("Failed to read body: {:?}", &e);
        ack_dispatcher(request);
        return Err(EventError::MalformedRequest(format!(
            "Failed to read body: {:?}",
            e
        )));
    }
    // Regardless of whether we successfully deserialize, we should ack the dispatcher so they don't keep resending it
    ack_dispatcher(request);
    let json_event: E = serde_json::from_slice(body.as_bytes())
        .map_err(|e| EventError::Deserialize(format!("Could not decode body to JSON: {:?}", e)))?;

    let signer_event: SignerEvent<T> = json_event.try_into()?;

    Ok(signer_event)
}
```

**File:** libsigner/src/events.rs (L580-614)
```rust
            let messages: Vec<_> = event
                .modified_slots
                .iter()
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
                    let Ok(pk) = chunk.recover_pk() else {
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    let Ok(message) = read_next::<T, _>(&mut &chunk.data[..]) else {
                        warn!(
                            "Skipping signer chunk: payload deserialization failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    Some((chunk.slot_id, pk, message))
                })
                .collect();
```

**File:** stacks-signer/src/lib.rs (L119-132)
```rust
impl<S: Signer<T> + Send + 'static, T: SignerEventTrait + 'static> SpawnedSigner<S, T> {
    /// Create a new spawned signer
    pub fn new(config: GlobalConfig) -> Self {
        let endpoint = config.endpoint;
        info!("Stacks signer version {:?}", VERSION_STRING.as_str());
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
