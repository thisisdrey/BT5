Verified. The critical broken invariant is in `stackslib/src/net/atlas/download.rs`'s `extend_with_attachments` / `AttachmentsBatchStateMachine::try_proceed` flow: the response to an `AttachmentRequest` is never checked against the `content_hash` that was actually requested, and the resulting attachment is unconditionally persisted as "instantiated" (validated) regardless of whether any `AttachmentInstance` references its real hash.This confirms the finding, but note the audit question's specific framing ("no on-chain BNS commitment matches this hash") and its precondition are already established via the companion finding — that a mismatched attachment can enter `AtlasDB` unvalidated. Given that, `getattachment.rs`'s handler serves it uncritically.

### Title
`GET /v2/attachments/{hash}` serves attacker-poisoned, unvalidated `AtlasDB` content as canonical BNS attachment data - (File: `stackslib/src/net/api/getattachment.rs`)

### Summary
`RPCGetAttachmentRequestHandler::try_handle_request` trusts `AtlasDB::find_attachment` unconditionally, returning any row marked `was_instantiated = 1` as a validated `GetAttachmentResponse` with `HTTP 200`. Because the Atlas downloader (`stackslib/src/net/atlas/download.rs`) marks attachments as instantiated without ever checking that the peer's response content hashes to the `content_hash` that was actually requested, a malicious peer can plant arbitrary `(hash(X), X)` pairs into a victim's `AtlasDB`, which the RPC endpoint then serves to any client as if it were an on-chain-attested attachment.

### Finding Description
The broken equality: bytes served for `attachment_hash` at `/v2/attachments/{attachment_hash}` are expected to equal the content committed by a confirmed on-chain `AttachmentInstance` (i.e., `content_hash` derived from a BNS name-update transaction). This equality is violated.

Root cause is in the Atlas attachment-download pipeline, not in `getattachment.rs` itself, but `getattachment.rs` is the endpoint that surfaces the poisoned state:

1. `AttachmentsBatchStateContext::extend_with_attachments` (`stackslib/src/net/atlas/download.rs:530-558`) decodes a peer's HTTP response into an `Attachment` and inserts it into `self.attachments` (a `HashSet<Attachment>`) with **no check** that `response.attachment.hash()` matches the `AttachmentRequest.content_hash` that was requested [1](#0-0) .
2. When the state machine reaches `Done`, for every attachment in `context.attachments.drain()`, the code looks up `find_all_attachment_instances(&attachment.hash())` (using the hash of the *actual received bytes*, not the originally requested hash) and then calls `network.atlasdb.insert_instantiated_attachment(&attachment)` **unconditionally**, regardless of whether any matching `AttachmentInstance` was found [2](#0-1) .
3. `insert_instantiated_attachment` writes the row with `was_instantiated = 1` [3](#0-2) , and `AtlasDB::find_attachment` only filters on `was_instantiated = 1`, with no cross-check against `attachment_instances` [4](#0-3) .
4. `RPCGetAttachmentRequestHandler::try_handle_request` calls exactly this `find_attachment` and, on `Ok(Some(attachment))`, serves it via `HttpResponsePreamble::ok_json` with no further validation [5](#0-4) .

Attacker's exact message: the malicious peer is selected as a `source` for a legitimate, on-chain-tracked `content_hash` (by advertising it in its `/v2/attachments/inv` response, per `get_prioritized_attachments_requests` at `stackslib/src/net/atlas/download.rs:404-478`). When the victim issues `GET /v2/attachments/{content_hash}` to that peer (`AttachmentRequest::make_request_type`, `stackslib/src/net/atlas/download.rs:1104-1118`), the attacker replies with a JSON `GetAttachmentResponse{ attachment: { content: <arbitrary bytes X> } }`. Since `X`'s hash almost certainly differs from the requested `content_hash`, and the code never checks this, `hash(X)` is stored as an "instantiated" attachment with no `AttachmentInstance` referencing it.

Existing guards fail because: `decode_atlas_get_attachment` only validates JSON well-formedness, not hash equality; `insert_instantiated_attachment` is called unconditionally rather than gated on `attachments_instances` being non-empty; and `find_attachment`/`getattachment.rs` have no secondary check against confirmed `AttachmentInstance` records at serve time.

### Impact Explanation
Any RPC client (unprivileged, remote) querying `GET /v2/attachments/{hash(X)}` on the victim node receives attacker-chosen bytes `X` with a `200 OK`, indistinguishable from a genuinely committed BNS attachment/zonefile. This is a BNS attachment-data mismatch: state is served that no canonical on-chain transaction committed, matching the "High" impact category (serving non-canonical state as canonical / attachment-BNS mismatch). It is repeatable for any hash the attacker chooses to forge (bounded only by needing to be selected as an inventory source for some real, pending `AttachmentInstance`), and affects every RPC caller of the poisoned node, not just the attacker.

### Likelihood Explanation
Preconditions: the victim node must be actively syncing Atlas attachments (normal operation for any BNS-aware node) and must select the attacker as a download source for some outstanding `AttachmentInstance` (achieved by simply claiming, via `/v2/attachments/inv`, to have the relevant page bit set — no signature or proof required). The attacker needs no privileged role, no RPC secret, and can be an ordinary outbound-connected P2P peer. Cost is a single crafted HTTP response; the attack is fully repeatable across many hashes/attachments.

### Recommendation
In `AttachmentsBatchStateContext::extend_with_attachments`, verify `response.attachment.hash() == request.content_hash` before accepting the response (treat mismatches as a failed request, similar to malformed JSON). Additionally, in the `Done` handler of `AttachmentsDownloadState::try_proceed` (`download.rs:153-165`), only call `insert_instantiated_attachment` when `attachments_instances` (or an equivalent check against known `AttachmentInstance` `content_hash`) is non-empty, so that content with no corresponding on-chain-tracked hash is never persisted as validated.

### Proof of Concept
Rust test plan (place near `stackslib/src/net/atlas/tests.rs` or as an integration test using the harness in `stackslib/src/net/atlas/download.rs`'s test module):
1. Seed an `AtlasDB` with one queued `AttachmentInstance` for `content_hash = H_real` (simulating a real on-chain BNS commitment), via `queue_attachment_instance`.
2. Simulate the download flow: construct an `AttachmentRequest{ content_hash: H_real, .. }` and feed it through `AttachmentsBatchStateContext::extend_with_attachments` with a mocked `BatchedRequestsResult` whose succeeded response decodes to `Attachment{ content: b"attacker-chosen-bytes" }` (hash `H_fake != H_real`).
3. Drive `try_proceed` to `Done` and confirm `atlasdb.find_all_attachment_instances(&H_fake)` returns empty (no instance references it) yet `atlasdb.find_attachment(&H_fake)` returns `Ok(Some(Attachment{content: b"attacker-chosen-bytes"}))` after `insert_instantiated_attachment` runs.
4. Construct `RPCGetAttachmentRequestHandler` with `attachment_hash = H_fake`, call `try_handle_request`, and assert the response is `200 OK` with `GetAttachmentResponse.attachment.content == b"attacker-chosen-bytes"` — i.e. assert this is NOT the `404 Unable to find attachment` path seen in the existing negative test at [6](#0-5) .

### Citations

**File:** stackslib/src/net/atlas/download.rs (L153-165)
```rust
            AttachmentsBatchStateMachine::Done(ref mut context) => {
                for attachment in context.attachments.drain() {
                    let attachments_instances = network
                        .atlasdb
                        .find_all_attachment_instances(&attachment.hash())
                        .map_err(net_error::DBError)?;
                    network
                        .atlasdb
                        .insert_instantiated_attachment(&attachment)
                        .map_err(net_error::DBError)?;
                    for attachment_instance in attachments_instances.into_iter() {
                        resolved_attachments.push((attachment_instance, attachment.clone()));
                    }
```

**File:** stackslib/src/net/atlas/download.rs (L530-558)
```rust
    pub fn extend_with_attachments(
        mut self,
        results: &mut BatchedRequestsResult<AttachmentRequest>,
    ) -> AttachmentsBatchStateContext {
        for (request, response) in results.succeeded.drain() {
            let report = self
                .peers
                .get_mut(request.get_url())
                .expect("Atlas: unable to retrieve reliability report for peer");

            let response = if let Some(r) = response {
                r
            } else {
                report.bump_failed_requests();
                continue;
            };

            if let Ok(response) = response.decode_atlas_get_attachment() {
                self.attachments.insert(response.attachment);
                report.bump_successful_requests();
            } else {
                report.bump_failed_requests();
            }
        }
        let mut events_ids = results.faulty_peers.keys().copied().collect::<Vec<usize>>();
        self.events_to_deregister.append(&mut events_ids);

        self
    }
```

**File:** stackslib/src/net/atlas/db.rs (L576-592)
```rust
    pub fn insert_instantiated_attachment(
        &mut self,
        attachment: &Attachment,
    ) -> Result<(), db_error> {
        let now = util::get_epoch_time_secs() as i64;
        let tx = self.tx_begin()?;
        tx.execute(
            "INSERT OR REPLACE INTO attachments (hash, content, was_instantiated, created_at) VALUES (?, ?, 1, ?)",
            params![attachment.hash(), attachment.content, now],
        )?;
        tx.execute(
            "UPDATE attachment_instances SET is_available = 1 WHERE content_hash = ?1 AND status = ?2",
            params![attachment.hash(), AttachmentInstanceStatus::Checked],
        )?;
        tx.commit()?;
        Ok(())
    }
```

**File:** stackslib/src/net/atlas/db.rs (L641-648)
```rust
    pub fn find_attachment(&self, content_hash: &Hash160) -> Result<Option<Attachment>, db_error> {
        let hex_content_hash = to_hex(&content_hash.0[..]);
        let qry = "SELECT content, hash FROM attachments WHERE hash = ?1 AND was_instantiated = 1"
            .to_string();
        let args = params![hex_content_hash];
        let row = query_row::<Attachment, _>(&self.conn, &qry, args)?;
        Ok(row)
    }
```

**File:** stackslib/src/net/api/getattachment.rs (L104-129)
```rust
        let attachment_res = node.with_node_state(
            |network, _sortdb, _chainstate, _mempool, _rpc_args| match network
                .get_atlasdb()
                .find_attachment(&attachment_hash)
            {
                Ok(Some(attachment)) => Ok(GetAttachmentResponse { attachment }),
                _ => {
                    let msg = "Unable to find attachment".to_string();
                    warn!("{msg}");
                    Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new(msg),
                    ))
                }
            },
        );
        let attachment = match attachment_res {
            Ok(attachment) => attachment,
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };

        let preamble = HttpResponsePreamble::ok_json(&preamble);
        let body = HttpResponseContents::try_from_json(&attachment)?;
        Ok((preamble, body))
```

**File:** stackslib/src/net/api/tests/getattachment.rs (L76-99)
```rust
    // query non-existant
    let request = StacksHttpRequest::new_getattachment(addr.into(), Hash160([0x22; 20]));
    requests.push(request);

    let mut responses = test_rpc(function_name!(), requests);

    let response = responses.remove(0);
    debug!(
        "Response:\n{}\n",
        std::str::from_utf8(&response.try_serialize().unwrap()).unwrap()
    );

    let resp = response.decode_atlas_get_attachment().unwrap();
    assert_eq!(resp.attachment, attachment);

    let response = responses.remove(0);
    debug!(
        "Response:\n{}\n",
        std::str::from_utf8(&response.try_serialize().unwrap()).unwrap()
    );

    let (preamble, body) = response.destruct();
    assert_eq!(preamble.status_code, 404);
}
```
