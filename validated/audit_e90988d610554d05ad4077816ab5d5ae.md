### Title
Network-wide block relay triggered by unauthenticated POST /v3/blocks/upload/ regardless of `broadcast`/auth flags - ([File: stackslib/src/net/api/postblock_v3.rs])

### Summary
`try_parse_request` correctly enforces that `broadcast=1` requires a valid `authorization` header, but `try_handle_request` never re-checks `self.broadcast` (or authentication) before calling `node.set_relay_message`. Relay is gated solely on `data_resp.accepted`, which is derived from `Relayer::process_new_nakamoto_block_ext(...)`'s chainstate-acceptance result, so any accepted block — submitted with no `broadcast` param and no `authorization` header — is still queued for network-wide propagation.

### Finding Description
The claimed intended equality is `{blocks entering set_relay_message} == {blocks whose submission carried broadcast==true && authenticated==true}`. Tracing the code:

- `try_parse_request` computes `broadcast` from the query string and `authenticated` from the `authorization` header, and rejects the request only when `broadcast && !authenticated` [1](#0-0) . When `broadcast` is absent (defaults to `false`), this check is skipped entirely regardless of authentication state, and `self.broadcast = Some(false)` is stored [2](#0-1) .
- `try_handle_request` calls `Relayer::process_new_nakamoto_block_ext(..., self.broadcast.unwrap_or(false))` unconditionally, and then sets `data_resp.accepted = accepted.is_accepted()` based purely on the chainstate-acceptance outcome, with no reference to `self.broadcast` or authentication [3](#0-2) .
- The relay trigger checks only `data_resp.accepted`, never `self.broadcast`: `if data_resp.accepted { node.set_relay_message(StacksMessageType::NakamotoBlocks(...)) }` [4](#0-3) .

Consequently, the `broadcast` parameter passed into `process_new_nakamoto_block_ext` only affects internal processing behavior of that function (e.g., whether it also directly announces via the relayer internally), but it does **not** gate the HTTP handler's own `set_relay_message` call, which is exactly the mechanism that pushes the block out to the node's network relay pipeline. Any remote, unauthenticated peer who submits a genuinely valid `NakamotoBlock` (one that legitimately passes `process_new_nakamoto_block_ext`'s chainstate/consensus checks) with no `broadcast` query argument and no `authorization` header will still cause `data_resp.accepted == true` and thus `set_relay_message` fires. The `broadcast`+auth gate implemented in `try_parse_request` is therefore cosmetic with respect to the actual relay side-effect — it only blocks the request when the attacker explicitly asks for `broadcast=1`, but an attacker can simply omit the parameter to reach the same relay outcome.

Note: an attacker cannot forge the cryptographic contents of a block (signer signatures, tenure linkage, etc.) — that acceptance logic is legitimate consensus validation and out of scope. But the finding here is not about forging block validity; it's that the endpoint's documented/coded access-control model ("broadcast requires the secret") is bypassed by the trivial act of omitting the query argument, since the code path that actually performs the relay side effect never consults the authentication/broadcast intent at all.

### Impact Explanation
Any remote, unauthenticated caller who possesses (or receives out-of-band) a chain-valid `NakamotoBlock` — e.g., one seen once via StackerDB/P2P gossip but not yet forwarded further — can force the target node to re-broadcast/relay it network-wide via `set_relay_message`, without presenting the RPC secret. This defeats the intended authorization boundary (only `broadcast=1` + secret should trigger propagation) and lets an unprivileged party command a node's relay behavior. This is an authorization-bypass on the relay-trigger control, matching the Critical category ("network-wide propagation ... auth bypass").

### Likelihood Explanation
- Attacker only needs unauthenticated RPC connectivity to the target's HTTP API (`/v3/blocks/upload/`), which is remotely reachable by design.
- Attacker needs a syntactically-valid, chainstate-acceptable `NakamotoBlock` payload (i.e., a real block, not an arbitrary forgery) — obtaining one does not require holding the node's secret or any privileged role, since blocks are gossiped/publicly observable once produced.
- No special peer/config state is required beyond the target node running with the `/v3/blocks/upload/` endpoint enabled (default RPC surface).
- Repeatable for every block the attacker can obtain.

### Recommendation
Gate the `set_relay_message` call (and/or the call into `process_new_nakamoto_block_ext`'s broadcast argument) on both conditions actually intended: `self.broadcast == Some(true) && authenticated`. Store the `authenticated` flag on the handler (not just `broadcast`) during `try_parse_request`, and check `if data_resp.accepted && self.authenticated && self.broadcast.unwrap_or(false) { node.set_relay_message(...) }`, so relay is only triggered for authenticated, explicit broadcast requests, consistent with the access-control check already performed in `try_parse_request`.

### Proof of Concept
Rust test in `stackslib::net::api::tests::postblock_v3`:
1. Construct `RPCPostBlockRequestHandler::new(Some("secret".into()))`.
2. Build a valid `NakamotoBlock` that will pass `Relayer::process_new_nakamoto_block_ext` (matching current tip/consensus state in the test harness).
3. Craft an HTTP POST to `/v3/blocks/upload/` with **no query string** (so `broadcast=false`) and **no `authorization` header**.
4. Call `try_parse_request` — assert it succeeds (no 401), confirming the auth-gate is skipped when `broadcast` is absent.
5. Call `try_handle_request` on a `StacksNodeState` wired to accept the block; assert `data_resp.accepted == true`.
6. Assert that `node.get_relay_message()` (or equivalent test hook capturing calls to `set_relay_message`) now contains `StacksMessageType::NakamotoBlocks(...)` for the submitted block, despite `self.broadcast == Some(false)` and no authentication having been presented — confirming the auth/broadcast gate is bypassed for the relay side effect.

### Citations

**File:** stackslib/src/net/api/postblock_v3.rs (L99-134)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }

        if Some(HttpContentType::Bytes) != preamble.content_type || preamble.content_type.is_none()
        {
            return Err(Error::DecodeError(
                "Invalid Http request: PostBlock takes application/octet-stream".to_string(),
            ));
        }

        let block = Self::parse_postblock_octets(body)?;

        self.block = Some(block);
        self.broadcast = Some(broadcast);
```

**File:** stackslib/src/net/api/postblock_v3.rs (L159-195)
```rust
        let response = node
            .with_node_state(|network, sortdb, chainstate, _mempool, rpc_args| {
                let mut handle_conn = sortdb.index_handle_at_tip();
                let stacks_tip = network.stacks_tip.block_id();
                Relayer::process_new_nakamoto_block_ext(
                    &network.burnchain,
                    sortdb,
                    &mut handle_conn,
                    chainstate,
                    &stacks_tip,
                    &block,
                    rpc_args.coord_comms,
                    NakamotoBlockObtainMethod::Uploaded,
                    self.broadcast.unwrap_or(false),
                )
            })
            .map_err(|e| {
                StacksHttpResponse::new_error(&preamble, &HttpError::new(400, e.to_string()))
            });

        let data_resp = match response {
            Ok(accepted) => {
                debug!(
                    "Received POSTed Nakamoto block {}/{}: {:?}",
                    &block.header.consensus_hash,
                    &block.header.block_hash(),
                    &accepted
                );
                StacksBlockAcceptedData {
                    accepted: accepted.is_accepted(),
                    stacks_block_id: block.block_id(),
                }
            }
            Err(e) => {
                return e.try_into_contents().map_err(NetError::from);
            }
        };
```

**File:** stackslib/src/net/api/postblock_v3.rs (L197-202)
```rust
        // should set to relay...
        if data_resp.accepted {
            node.set_relay_message(StacksMessageType::NakamotoBlocks(NakamotoBlocksData {
                blocks: vec![block],
            }));
        }
```
