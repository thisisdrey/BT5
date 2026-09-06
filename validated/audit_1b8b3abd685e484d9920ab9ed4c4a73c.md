## Finding

### Title
Signer's HTTP Event Receiver Accepts Unauthenticated POSTs, Allowing Forged Node Events to Be Injected — ([File: libsigner/src/events.rs])

### Summary
The documentation for signer/node communication (`docs/signing.md`) describes an `auth_token`/`auth_password` pair that is supposed to authenticate traffic between the Stacks node and the `stacks-signer` event listener [1](#0-0) . However, the actual implementation of the signer-side HTTP listener, `SignerEventReceiver::next_event()`, never validates any authentication header, token, or password before processing a request body as a trusted node event [2](#0-1) . A `grep` across `libsigner/**` for `auth_token`, `auth_password`, or `Authorization` returns zero matches, confirming there is no authentication check anywhere in this crate, whereas `auth_token` enforcement does exist on the node's RPC side (`stackslib/src/net/api/postblock_proposal.rs`, `stackslib/src/net/httpcore.rs`, `stackslib/src/net/connection.rs`) [3](#0-2) .

### Finding Description
`SignerEventReceiver::next_event()` accepts any HTTP POST reaching its bound socket and dispatches it based solely on the URL path — `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, or `/shutdown` — with no check of a shared secret, bearer token, or signature over the request: [4](#0-3) 

The body is then deserialized directly into a `SignerEvent` and forwarded to the signer's runloop as if it originated from the trusted local node: [5](#0-4) 

This breaks the equality the deployment model (and documentation) assumes: "data delivered on this endpoint == data that the paired, authenticated Stacks node produced." In reality, the endpoint accepts data from anyone who can reach the socket, since `bind()` merely opens the listener with no additional gating [6](#0-5) . This is the "auth-gate that fails open" analog called out in the rules: the security property is documented and configured (`auth_token = "..."` / `auth_password = "..."`) but is not actually enforced in the code path that consumes the data.

### Impact Explanation
An attacker who can reach the signer's listening socket (e.g., misconfigured to bind non-locally, as the sample config's `endpoint = "0.0.0.0:30000"` pattern suggests is a supported configuration [7](#0-6) , or via any network path to that port) can:
- Inject forged `StackerDBChunksEvent` payloads, causing the signer to process attacker-controlled StackerDB chunk data as if it came from the node's verified chunk store.
- Inject forged `BlockValidateResponse`/`BurnBlockEvent`/`StacksBlockEvent` messages, potentially steering the signer's internal state machine (e.g., causing it to believe a block was validated/rejected, or that a burn block occurred) using data that never passed through the node's own chainstate/consensus validation.
- Send a forged `/shutdown` request to terminate the signer's event receiver, a remote unauthenticated DoS of a critical component in block signing.

This matches the "unauthenticated/unauthorized write to state" and "forged-data propagation" impact categories in the rubric, since the signer treats the injected event as authoritative node output.

### Likelihood Explanation
Likelihood depends entirely on network exposure of the signer's bind address, which is an operator-controlled deployment detail; the code itself provides no defense-in-depth regardless of binding, so any reachable network path (including from other processes/containers on the same host, or a misconfigured firewall/bind address) is sufficient. The documentation's explicit mention of `auth_token`/`auth_password` matching as a required security control creates an expectation of protection that the code does not fulfill, making this a functional-equivalent of "fails open."

### Recommendation
Have `SignerEventReceiver` require and verify a shared secret (the documented `auth_token`/`auth_password`) via, e.g., an `Authorization` header on every incoming request (including `/status` and `/shutdown`) before processing the body, mirroring the pattern already used for node-side RPC endpoints in `stackslib/src/net/httpcore.rs` and `stackslib/src/net/api/postblock_proposal.rs`. Reject unauthenticated requests with 401/403 rather than passing them into `process_event`.

### Proof of Concept
1. Start a `stacks-signer` process bound to a reachable address/port (per the sample config pattern).
2. From an unauthenticated host with only network access to that port, send:
   ```
   POST /stackerdb_chunks HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <n>

   { "contract_id": "...", "modified_slots": [ ... attacker-controlled StackerDBChunkData ... ] }
   ```
3. Observe that `SignerEventReceiver::next_event()` accepts and deserializes this into a `SignerEvent` and forwards it to the signer runloop with no authentication check, as shown in `libsigner/src/events.rs:413-459` and `libsigner/src/events.rs:519-542`.
4. Alternatively, `POST /shutdown` to remotely terminate the signer's event loop with no credentials required.

**Note on scope/limitations:** I was unable to fully trace how the node-side `events_observer` actually sends its `auth_token` (or whether it does so at all) toward this signer endpoint, since that logic lives in the event-dispatch code that pushes observer events out to configured endpoints (outside the files directly returned by my searches). If such logic exists and is enforced only optionally, that would further corroborate this finding, but I could not directly confirm the sender side within the scoped search. This does not change the core finding: the receiver itself (in-scope `libsigner` transport code) performs no verification regardless of what the sender does.

### Citations

**File:** docs/signing.md (L26-49)
```markdown
[node]
stacker = true

[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

[connection_options]
auth_token = "your-secret-token"
```

### 2. Configure the Signer

Use [`mainnet-signer-conf.toml`](../sample/conf/signer/mainnet-signer-conf.toml) as a starting point.
Key settings:

```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```
```

**File:** libsigner/src/events.rs (L401-408)
```rust
    /// Start listening on the given socket address.
    /// Returns the address that was bound.
    /// Errors out if bind(2) fails
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L413-459)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
            debug!("Request handling");
            let request = http_server.recv()?;
            debug!("Got request"; "method" => %request.method(), "path" => request.url());

            if request.url() == "/status" {
                request
                .respond(HttpResponse::from_string("OK"))
                .expect("response failed");
                return Ok(SignerEvent::StatusCheck);
            }

            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
            debug!("Processing {} event", request.url());
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
                process_event::<T, StacksBlockEvent>(request)
            } else {
                let url = request.url().to_string();
                debug!(
                    "[{:?}] next_event got request with unexpected url {}, return OK so other side doesn't keep sending this",
                    event_receiver.local_addr,
                    url
                );
                ack_dispatcher(request);
                Err(EventError::UnrecognizedEvent(url))
            }
        })?
    }
```

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

**File:** stackslib/src/net/api/postblock_proposal.rs (L347-382)
```rust
impl NakamotoBlockProposal {
    fn spawn_validation_thread(
        self,
        sortdb: SortitionDB,
        mut chainstate: StacksChainState,
        receiver: Box<dyn ProposalCallbackReceiver>,
        connection_opts: &ConnectionOptions,
    ) -> Result<JoinHandle<()>, std::io::Error> {
        let timeout_secs = connection_opts.block_proposal_validation_timeout_secs;
        let max_tx_execution_time_secs = connection_opts.block_proposal_max_tx_execution_time_secs;
        let max_tx_analysis_time_secs = connection_opts.block_proposal_max_tx_analysis_time_secs;
        let max_tx_mem_bytes = connection_opts.block_proposal_max_tx_mem_bytes;
        let auth_token = connection_opts.auth_token.clone();
        thread::Builder::new()
            .name("block-proposal".into())
            .spawn(move || {
                let result = self
                    .validate(
                        &sortdb,
                        &mut chainstate,
                        timeout_secs,
                        max_tx_execution_time_secs,
                        max_tx_analysis_time_secs,
                        max_tx_mem_bytes,
                        auth_token,
                    )
                    .map_err(|reason| BlockValidateReject {
                        signer_signature_hash: self.block.header.signer_signature_hash(),
                        reason_code: reason.reason_code,
                        reason: reason.reason,
                        failed_txid: reason.failed_txid,
                    });
                receiver.notify_proposal_result(result);
            })
    }

```
