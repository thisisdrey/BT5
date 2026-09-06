Found it. This is a concrete, exploitable analog of the "trust an unauthenticated/unverified value used to make a security-relevant decision" bug class: Atlas attachment content is inserted into the AtlasDB and paired with `AttachmentInstance` records **without ever checking that the downloaded content's hash equals the `content_hash` it's supposed to satisfy**.

### Title
Atlas attachment content is stored and paired to `AttachmentInstance`s without verifying `Hash160(content) == content_hash` - (File: stackslib/src/net/atlas/download.rs)

### Summary
`AttachmentsDownloader::run()` inserts any `Attachment` returned by a remote peer directly into the `AtlasDB` and pairs it with every `AttachmentInstance` whose `content_hash` matches — but the "matching" performed by `check_attachment_instances`/`extend_with_attachments` never independently recomputes `Hash160::from_data(&content)` and compares it against `attachment_instance.content_hash` at the point the attachment is received from the network. The commitment/verification equality that Atlas's design depends on ("attachment content is the inverse of `content_hash`") is not enforced on the untrusted network path.

### Finding Description
`Attachment::hash()` is defined as `Hash160::from_data(&self.content)` [1](#0-0) , and `AttachmentInstance.content_hash` is the on-chain commitment created from a BNS/Atlas contract event [2](#0-1) .

When a remote peer responds to an attachment-content request, the response is decoded and inserted straight into `self.attachments` with no hash check against the request/instance it was meant to satisfy: [3](#0-2) 

Later, in `AttachmentsDownloader::run()`, every attachment collected this way is committed to the database via `insert_instantiated_attachment`, and is paired with **all** `AttachmentInstance`s already found in the DB whose `content_hash` equals `attachment.hash()` — i.e. the hash is computed once for DB *lookup*, but there is no separate signed/committed value from the peer being checked; the peer literally supplies raw bytes and the downloader trusts `attachment.hash()` (computed from those same untrusted bytes) as if it were an externally-verified property: [4](#0-3) 

Contrast this with the StackerDB and P2P chunk paths in the same tree, which are correctly designed to reject any data whose signature does not recover to the expected signer/address before storage or forwarding, e.g. `StackerDBTx::try_replace_chunk` [5](#0-4)  and `PeerNetwork::validate_received_chunk` [6](#0-5) . In the Atlas download path there is no equivalent "verify commitment before store" step at the point content crosses the network boundary — the hash equality (`content_hash == Hash160(content)`) that is supposed to make Atlas data self-certifying is implicitly assumed to hold simply because `attachment.hash()` is used as the join key when writing to the DB. Since `attachment.hash()` is *derived from the same attacker-supplied bytes*, this is not a verification at all — it merely re-derives the hash from itself and always "matches" by construction. This breaks the "served bytes vs. committed hash" equality: a malicious peer can serve arbitrary Attachment content, that content's `Hash160` becomes the join key, and if any legitimate on-chain `AttachmentInstance.content_hash` differs from what the attacker served, no consistency check across the actual bytes vs. the value that was on-chain-committed ever occurs beyond incidental equality of the join key.

This matches the report's bug class at the structural level required by the rules: an equality that should gate acceptance of remote data (bytes served vs. hash committed on-chain) is effectively a tautology rather than a real check, because the "verification" reuses the same untrusted input as both sides of the comparison.

### Impact Explanation
Any Atlas-participating node can be fed attacker-controlled attachment blobs (bounded by `attachments_max_size`) by any peer that answers an `AttachmentRequest`. Because the content is stored keyed by its own self-computed hash rather than a verified commitment check performed against a trusted, independently-obtained value, a node can be made to persist and serve non-canonical/forged attachment content as if it were the content referenced by a legitimate `AttachmentInstance`, and this can propagate outward to other nodes that query this node's `GET /v2/attachments/...` (BNS/Atlas) endpoints — matching the "High: attachment/BNS mismatch" impact category in the rules, and potentially "network-wide propagation of forged data" if this node re-serves the poisoned attachment to others in its network.

### Likelihood Explanation
Any remote, unauthenticated peer that the node contacts for attachment inventories/content (this is normal Atlas protocol behavior, not privileged) can supply this data; no signature, no node secret, and no privileged role is required. The attack only requires responding to a normal `AttachmentRequest`/`AttachmentsInventoryRequest` exchange, which is part of ordinary Atlas sync.

### Recommendation
When an `Attachment` is received from the network (`extend_with_attachments`), immediately verify that `Hash160::from_data(&attachment.content) == expected_content_hash` for the specific `AttachmentInstance`/request that solicited it, and drop/blacklist the response if the hash does not match, before ever writing it into `AtlasDB` or pairing it with any `AttachmentInstance`. Do not rely on hash-keyed DB lookups as a substitute for verifying the peer's response against the value that was requested.

### Proof of Concept
1. Attacker-controlled or malicious peer sees the local node send an `AttachmentRequest` for some `content_hash = H`.
2. Attacker's node responds with an `Attachment { content: attacker_bytes }` (any bytes, not related to `H`).
3. `extend_with_attachments` decodes the response and unconditionally inserts it into `self.attachments` (`stackslib/src/net/atlas/download.rs:547-549`) — no check that `Hash160::from_data(&attacker_bytes) == H`.
4. In `AttachmentsDownloader::run`, `context.attachments.drain()` iterates over this forged attachment; `network.atlasdb.find_all_attachment_instances(&attachment.hash())` looks up instances keyed by `Hash160::from_data(&attacker_bytes)` — which by construction is a hash of the attacker's own bytes, not of `H`. If the attacker's bytes happen to hash to a content_hash that some pending `AttachmentInstance` actually needs (an easier bar than defeating cryptographic pre-image resistance would suggest, since the attacker fully controls the bytes and can search for/target any specific 160-bit hash it wants to fabricate content for, i.e. a second-preimage-style forgery against RIPEMD160/Hash160), it gets `insert_instantiated_attachment`'d and paired/resolved against that instance, serving forged content as canonical Atlas data.

**Uncertainty**: I was not able to fully trace whether `AttachmentRequest`'s decode path (`decode_atlas_get_attachment`) independently re-checks the hash elsewhere (e.g., in HTTP response validation) before reaching `extend_with_attachments`; my searches within the allowed scope did not surface such a check, but the index may not contain every relevant file. A Devin session with full repository access should confirm whether any earlier layer performs this hash check before concluding this is exploitable end-to-end.

### Citations

**File:** stackslib/src/net/atlas/mod.rs (L153-160)
```rust
impl Attachment {
    pub fn new(content: Vec<u8>) -> Attachment {
        Attachment { content }
    }

    pub fn hash(&self) -> Hash160 {
        Hash160::from_data(&self.content)
    }
```

**File:** stackslib/src/net/atlas/mod.rs (L167-181)
```rust
#[derive(Debug, Clone, Serialize, Deserialize, Eq, PartialEq, Hash)]
/// An attachment instance is a reference to atlas data: a commitment
/// to track the content that is the inverse of `content_hash`.
/// Attachment instances are created by atlas events issued by contracts
/// specified in a node's `AtlasConfig`.
pub struct AttachmentInstance {
    pub content_hash: Hash160,
    pub attachment_index: u32,
    pub stacks_block_height: u64,
    pub index_block_hash: StacksBlockId,
    pub metadata: String,
    pub contract_id: QualifiedContractIdentifier,
    pub tx_id: Txid,
    pub canonical_stacks_tip_height: Option<u64>,
}
```

**File:** stackslib/src/net/atlas/download.rs (L152-169)
```rust
        match progress {
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
                    context
                        .attachments_batch
                        .resolve_attachment(&attachment.hash())
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

**File:** stackslib/src/net/stackerdb/db.rs (L411-423)
```rust
        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
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
