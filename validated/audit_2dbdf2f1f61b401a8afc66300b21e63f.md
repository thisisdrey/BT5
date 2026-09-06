### Title
Unauthenticated forged block-validation verdict injection via signer's `/proposal_response` event endpoint - ([File: libsigner/src/events.rs])

### Summary
The signer's event-receiver HTTP server (`SignerEventReceiver::next_event`) accepts any POST to `/proposal_response` and deserializes the body directly into a `BlockValidateResponse`, which is unconditionally wrapped into `SignerEvent::BlockValidationResponse` and forwarded to the runloop with no signature, token, or origin check. Any TCP peer able to reach the bound socket can inject a fabricated validation verdict indistinguishable from one the node's actual block validator produced.

### Finding Description
The equality that should hold — "`SignerEvent::BlockValidationResponse` content == the node's real `/v3/block_proposal` validation result" — is broken. `next_event` routes any POST whose path is `/proposal_response` straight into `process_event::<T, BlockValidateResponse>(request)` [1](#0-0) , and this occurs inside `with_server`, which only checks whether the HTTP server has been bound — no authentication check exists anywhere in this call chain [2](#0-1) . `SignerEvent::BlockValidationResponse(BlockValidateResponse)` is a plain variant with no accompanying signature or origin metadata [3](#0-2) . The socket itself is opened via a bare `tiny_http::Server`/`HttpServer::http(listener)` with no TLS, no bearer-token gate, and no peer allow-list [4](#0-3) . Sample configs bind this socket to `0.0.0.0:30000` [5](#0-4) , i.e. all interfaces, not loopback-only. `forward_event` then pushes whatever `SignerEvent` was constructed straight to the signer runloop channel with zero further vetting [6](#0-5) .

The `auth_token`/`auth_password` pair documented throughout the sample configs and `docs/signing.md` only authenticates the signer's *outbound* calls to the node's RPC (e.g., `/v3/block_proposal` submission), not inbound POSTs the node makes to the signer's event listener — that path has no analogous secret at all.

This gap is explicitly acknowledged by the codebase itself: `SpawnedSigner::new` logs a warning that "the signer is primarily designed for use with a local or subnet network stacks node" and that communicating with an external/untrusted node "could potentially expose sensitive data or functionalities to security risks if additional proper security checks are not integrated" [7](#0-6) . That warning frames the endpoint's node-originated traffic as untrusted-by-default and confirms no code-level guard exists.

Attacker's exact message: a raw TCP connection to the signer's bound port sending
```
POST /proposal_response HTTP/1.1
Host: <signer-host>:30000
Content-Type: application/json
Content-Length: <n>

{"result":"Rejected","reason":"...","reason_code":"...","signer_signature_hash":"..."}
```
(or the `"result":"Valid"` OK-shaped body). `tiny_http` parses this as any other request, `process_event` deserializes the JSON into `BlockValidateResponse`, wraps it in `SignerEvent::BlockValidationResponse`, and `forward_event` places it on the mpsc channel consumed by the signer runloop — exactly as if the node's own validator had produced it.

### Impact Explanation
This is an unauthenticated write into the signer's internal event stream: a forged `BlockValidationResponse` is accepted and forwarded to the runloop unchanged, satisfying the "network-wide propagation of forged data" / "unauthenticated write to state" category. The downstream consequences of feeding the runloop a bogus validation verdict (e.g. driving `handle_block_validate_response` in `stacks-signer/src/v0/signer.rs`) are explicitly out of scope per the prompt, but the transport-level injection itself is Critical: it defeats the entire premise that `BlockValidationResponse` reflects the node's actual chainstate-validator output, and is trivially repeatable per message from any reachable client, with no state, secret, or role required.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs network reachability to the signer's configured `endpoint` (bound in reference configs to `0.0.0.0:<port>`, i.e., not restricted to loopback) and the ability to open a TCP connection and send well-formed HTTP/1.1 with a JSON body matching `BlockValidateResponse`'s schema. No node RPC secret, StackerDB slot, or peer key is needed — the attacker never talks to the node at all, only to the signer's listener. This is remotely reachable with attacker cost of a single crafted HTTP POST, and is fully repeatable.

### Recommendation
Add an authentication/integrity check on the signer's event-receiver HTTP server: require a shared secret/bearer token (reusing or extending the existing `auth_token`/`auth_password` mechanism, but validated on the *inbound* signer-listener side) on every POST, and/or bind the listener to loopback/a private interface by default with an explicit opt-in warning to expose it further, and/or have the node produce a signed/MACed `BlockValidateResponse` that the signer verifies before constructing `SignerEvent::BlockValidationResponse`.

### Proof of Concept
Rust test plan (modeled on the existing `test_simple_signer`/`test_status_endpoint` patterns in `libsigner/src/tests/mod.rs`):
1. Spawn a `SignerEventReceiver<SignerMessage>` and `Signer::spawn` on `127.0.0.1:<port>` as in `test_status_endpoint`.
2. From a separate thread, open a bare `TcpStream::connect(endpoint)` (no node identity, no auth headers) and send:
   ```
   POST /proposal_response HTTP/1.1\r\nHost: <endpoint>\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: <n>\r\n\r\n<json>
   ```
   with `<json>` a hand-crafted `BlockValidateReject`-shaped or `BlockValidateOk`-shaped JSON never produced by any validator.
3. Stop the signer and collect `accepted_events` (as `test_status_endpoint` does via `running_signer.stop()`).
4. Assert `accepted_events` contains `SignerEvent::BlockValidationResponse(<exact forged value>)`, proving the value forwarded equals the attacker-supplied bytes with no verification performed.

### Citations

**File:** libsigner/src/events.rs (L220-221)
```rust
    /// A new block proposal validation response from the node
    BlockValidationResponse(BlockValidateResponse),
```

**File:** libsigner/src/events.rs (L342-358)
```rust
    /// Do something with the socket
    pub fn with_server<F, R>(&mut self, todo: F) -> Result<R, EventError>
    where
        F: FnOnce(&SignerEventReceiver<T>, &mut HttpServer, bool) -> R,
    {
        let mut server = if let Some(s) = self.http_server.take() {
            s
        } else {
            return Err(EventError::NotBound);
        };

        let res = todo(self, &mut server, self.is_mainnet);

        self.http_server = Some(server);
        Ok(res)
    }
}
```

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L437-441)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
```

**File:** libsigner/src/events.rs (L469-480)
```rust
    fn forward_event(&mut self, ev: SignerEvent<T>) -> bool {
        if self.out_channels.is_empty() {
            // nothing to do
            error!("No channels connected to event receiver");
            false
        } else if self.out_channels.len() == 1 {
            // avoid a clone
            if let Err(e) = self.out_channels[0].send(ev) {
                error!("Failed to send to signer runloop: {:?}", &e);
                return false;
            }
            true
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** stacks-signer/src/lib.rs (L125-132)
```rust
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
