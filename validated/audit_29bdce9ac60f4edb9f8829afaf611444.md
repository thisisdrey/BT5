## Analysis Result

Based on the investigation of `libsigner/src/events.rs`, I found a plausible remote unauthenticated DoS analog to the Magento WebAPI issue (auth-gate that fails open — here, an endpoint with *no* auth gate at all).

### Title
Unauthenticated remote shutdown of the signer event listener via `/shutdown` HTTP endpoint - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver`'s embedded `tiny_http` server, which listens for events pushed from the local Stacks node, exposes a `/shutdown` route that terminates the event-processing loop on receipt of *any* POST request to that path — with no authentication, token, or peer-identity check of any kind.

### Finding Description
`SignerEventReceiver::bind` starts a plain `tiny_http::Server` on the configured listener address [1](#0-0) . Inside `next_event`, the request-dispatch logic checks the URL path and, for `/shutdown`, immediately sets `stop_signal` and returns `EventError::Terminated`, with no verification that the request actually originated from the paired Stacks node process: [2](#0-1) 

A grep of the whole file for `Authorization`, `auth_token`, or `bearer` returns no matches — there is no credential, shared secret, or source-address check gating any of the routes (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`). Compare this to the StackerDB write path (`stackslib/src/net/stackerdb/mod.rs::validate_received_chunk`), which strictly requires a valid slot-owner signature before any state is accepted [3](#0-2)  — the signer's own local event-ingestion surface has no equivalent authentication concept at all.

### Impact Explanation
Any party capable of reaching the bound TCP port (which, depending on operator configuration, may not be restricted to loopback) can send a single unauthenticated `POST /shutdown` HTTP request and immediately halt the signer's event-receiver loop (`is_stopped()` becomes `true`, and `next_event` returns `Err(EventError::Terminated)` on every subsequent call). This stops the signer from ingesting further `StackerDBChunksEvent`, `BlockValidateResponse`, and `StacksBlockEvent` data pushed by the node, degrading or halting the signer's participation in block signing — a remote, unauthenticated denial-of-service achievable with a single crafted HTTP request, matching the "Critical – remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
Exploitation requires only network reachability to the signer's event-receiver port and a single well-formed HTTP POST; no cryptographic material, node secret, or elevated role is required. The actual likelihood in a given deployment depends on whether operators bind this listener to a non-loopback interface (which the code does not prevent or warn against) — I could not fully verify default bind-address behavior or any firewall guidance in the docs within the indexed content, so this should be treated as a configuration-dependent but code-enabled weakness rather than a universally remote-exploitable one.

### Recommendation
Add authentication to the event-receiver HTTP endpoints (e.g., a shared secret/bearer token configured between the node and the signer, or restrict `/shutdown` and other mutating routes to loopback-only bindings enforced in code, not just deployment convention). At minimum, remove or gate the `/shutdown` route so it cannot be triggered by an unauthenticated remote peer.

### Proof of Concept
```
POST /shutdown HTTP/1.1
Host: <signer-event-listener-ip>:<port>
Content-Length: 0

```
Sending this to the signer's configured event-receiver socket sets `stop_signal` and causes `next_event` to return `Err(EventError::Terminated)` on all subsequent polls, per [4](#0-3) .

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L437-446)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
                process_event::<T, BurnBlockEvent>(request)
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
```rust
        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }
```
