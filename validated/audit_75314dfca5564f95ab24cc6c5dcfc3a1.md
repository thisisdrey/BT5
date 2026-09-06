### Title
`AttachmentsBatchStateContext::extend_with_attachments` stores downloaded attachment content with no `attachments_max_size` bound check - (File: `stackslib/src/net/atlas/download.rs`)

### Summary
`extend_with_attachments` decodes a peer's `GetAttachmentResponse` via `decode_atlas_get_attachment()` and unconditionally inserts `response.attachment` into `self.attachments`, with no comparison against `AtlasConfig.attachments_max_size`. The only place in the Atlas code that enforces this cap, `AtlasDB::should_keep_attachment`, is not invoked anywhere on this download path, so attachment content flowing from `AttachmentRequest` responses into the node's SQLite `attachments` table via `insert_instantiated_attachment` is unbounded in size.

### Finding Description
The claimed equality `len(attachment.content) <= AtlasConfig.attachments_max_size` is violated on the P2P/RPC attachment download path. `RPCGetAttachmentRequestHandler`'s HTTP response decoder simply parses hex bytes into `Attachment::new(bytes)` with no length check [1](#0-0) , and `decode_atlas_get_attachment` does the same JSON round-trip with no size validation [2](#0-1) .

`extend_with_attachments` then takes this decoded response and stores it directly: [3](#0-2) 
There is no call to `AtlasDB::should_keep_attachment` (or any equivalent size check) before `self.attachments.insert(response.attachment)`. The only place that function is defined performs exactly the check the question is asking about — `attachment.content.len() as u32 > self.atlas_config.attachments_max_size` — but it lives in `db.rs` and is not referenced from the download state machine at all [4](#0-3) .

The root cause is architectural: the size cap defined on `AtlasConfig` (`attachments_max_size`, minimum `ATTACHMENTS_MAX_SIZE_MIN = 1_048_576` bytes) is a node-local policy, not a consensus-enforced commitment size limit [5](#0-4) . Any permissionless account can emit an `AttachmentInstance` event from a supported contract committing to an arbitrary `content_hash`; nothing at the chain level bounds the size of the preimage. Since the attacker picks the content first and then computes the Hash160 commitment they publish on-chain, they trivially control both the hash and the (oversized) content, and can serve that content when any node's `AttachmentsDownloader` requests it via `AttachmentRequest`. Because `extend_with_attachments` performs no size check, the oversized blob is admitted into `self.attachments` and subsequently persisted via `insert_instantiated_attachment` in `AtlasDB`.

### Impact Explanation
Every node that runs the Atlas attachment sync (any Stacks node with Atlas/BNS attachments enabled) can be induced to fetch and permanently store attacker-chosen, arbitrarily large attachment blobs into its local `atlas.sqlite` `attachments` table, unbounded by the configured `attachments_max_size`. This is an unauthenticated write of unbounded-size data into node state/storage, repeatable per attachment instance the attacker chooses to commit and serve, and can be used to exhaust disk space across all downloading nodes in the network — matching the "unauthenticated/unauthorized write to state" High/Critical impact category from a P2P/RPC-reachable, unprivileged actor.

### Likelihood Explanation
The precondition is only that the attacker can (a) submit a normal, permissionless Stacks transaction that triggers an `AttachmentInstance` event from a contract in `AtlasConfig.contracts` (e.g., the BNS contract), and (b) run/operate a peer node (or be selected as an outbound sync peer / data URL provider) that answers the corresponding `AttachmentRequest` with oversized content. Both actions require no privileged role, no RPC secret, and no signer/StackerDB key — they are standard permissionless blockchain interactions plus running your own peer, matching the described unprivileged threat model. The attack is fully repeatable and remotely reachable through `/v2/attachments/:hash`.

### Recommendation
Enforce the `attachments_max_size` bound in `extend_with_attachments` (and/or centrally in `decode_atlas_get_attachment`) before inserting into `self.attachments`: reject/discard responses where `response.attachment.content.len() as u32 > attachments_max_size`, bumping `report.bump_failed_requests()` instead of `bump_successful_requests()`. Additionally verify `Hash160::from_data(&content) == expected_content_hash` for the corresponding `AttachmentInstance` before accepting the attachment, and consider surfacing the same check inside `AtlasDB::insert_instantiated_attachment` itself as a defense-in-depth backstop so any future caller of that function cannot bypass the cap.

### Proof of Concept
In `stackslib::net::atlas::download::tests` (or a new test module), construct a `BatchedRequestsResult<AttachmentRequest>` whose `succeeded` map contains one `AttachmentRequest` paired with `Some(StacksHttpResponse)` built from a `GetAttachmentResponse { attachment: Attachment { content: vec![0u8; (attachments_max_size + 1) as usize] } }`. Call `AttachmentsBatchStateContext::extend_with_attachments` on a context whose `AtlasConfig.attachments_max_size` is small (e.g., the `ATTACHMENTS_MAX_SIZE_MIN`), then assert that `context.attachments` still contains the oversized `Attachment` (proving no size gate) — expected current behavior: the oversized attachment IS present in `self.attachments`, contradicting the intended bound; the fix should make this assertion fail (i.e., the oversized attachment must be absent).

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L69-77)
```rust
impl<'de> Deserialize<'de> for GetAttachmentResponse {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<GetAttachmentResponse, D::Error> {
        let payload = String::deserialize(d)?;
        let hex_encoded = payload.parse::<String>().map_err(de_Error::custom)?;
        let bytes = hex_bytes(&hex_encoded).map_err(de_Error::custom)?;
        let attachment = Attachment::new(bytes);
        Ok(GetAttachmentResponse { attachment })
    }
}
```

**File:** stackslib/src/net/atlas/mod.rs (L91-99)
```rust
#[derive(Debug, Clone, PartialEq)]
pub struct AtlasConfig {
    pub contracts: HashSet<QualifiedContractIdentifier>,
    pub attachments_max_size: u32,
    pub max_uninstantiated_attachments: u32,
    pub uninstantiated_attachments_expire_after: u32,
    pub unresolved_attachment_instances_expire_after: u32,
    pub genesis_attachments: Option<Vec<Attachment>>,
}
```

**File:** stackslib/src/net/api/getattachment.rs (L158-165)
```rust
impl StacksHttpResponse {
    pub fn decode_atlas_get_attachment(self) -> Result<GetAttachmentResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: GetAttachmentResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
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

**File:** stackslib/src/net/atlas/db.rs (L249-266)
```rust
    pub fn should_keep_attachment(
        &self,
        contract_id: &QualifiedContractIdentifier,
        attachment: &Attachment,
    ) -> bool {
        if !self.atlas_config.contracts.contains(contract_id) {
            info!(
                "Atlas: will discard posted attachment - {} not in supported contracts",
                contract_id
            );
            return false;
        }
        if attachment.content.len() as u32 > self.atlas_config.attachments_max_size {
            info!("Atlas: will discard posted attachment - attachment too large");
            return false;
        }
        true
    }
```
