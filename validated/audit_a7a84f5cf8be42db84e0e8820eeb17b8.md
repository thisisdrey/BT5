### Title
Unauthenticated storage exhaustion of Atlas attachment cache via unmatched attachments in `POST /v2/transactions` - (File: stackslib/src/net/api/posttransaction.rs)

### Summary
`RPCPostTransactionRequestHandler::try_handle_request` accepts an `attachment` field alongside any `ContractCall` transaction targeting an Atlas-tracked contract and stores it via `insert_uninstantiated_attachment` after only checking `should_keep_attachment` (contract allow-list + size), without verifying that the attachment's hash corresponds to any actual `AttachmentInstance.content_hash` committed by a confirmed transaction. This lets any unprivileged remote caller fill the bounded uninstantiated-attachment store with garbage, evicting legitimate attachments that are still waiting to be matched.

### Finding Description
In `try_handle_request` [1](#0-0) , once the accompanying transaction is accepted into the mempool, if it is a `ContractCall` to a contract in `AtlasConfig.contracts`, the handler calls `network.get_atlasdb().should_keep_attachment(&contract_id, attachment)` and, if true, unconditionally calls `insert_uninstantiated_attachment(attachment)`.

`should_keep_attachment` only checks that the contract is allow-listed and the attachment size is within `attachments_max_size` — it does not consult the `attachment_instances` table or otherwise validate that `attachment.hash()` matches any pending/queued `AttachmentInstance.content_hash`. The equality the question describes — that a stored attachment's hash should equal a hash actually committed by a confirmed name/BNS operation (an `AttachmentInstance`) — is never checked at insertion time in this code path. `AttachmentInstance` objects are only created from processed contract events (in `try_new_from_value`) [2](#0-1) , and reconciliation between stored attachments and instances happens asynchronously in the downloader, not at write time in `posttransaction.rs`.

Because the mempool transaction itself need not succeed at anything related to Atlas (any accepted `ContractCall` to an allow-listed contract works, e.g. BNS), and the `attachment` field is attacker-controlled arbitrary bytes, an attacker can:
1. Craft N distinct valid-but-harmless `ContractCall` transactions to the BNS contract (or any Atlas contract), each with a unique bogus attachment payload under `attachments_max_size`.
2. POST each via `/v2/transactions` with `Content-Type: application/json`, so `parse_posttransaction_json` decodes both `tx` and `attachment` [3](#0-2) .
3. Each request passes `mempool.has_tx` (unique txid), `static_check_problematic_relayed_tx`, and `mempool.submit`, then reaches the attachment-store branch and calls `insert_uninstantiated_attachment` unconditionally.

Existing guards (`should_keep_attachment`'s size/contract checks and `max_uninstantiated_attachments`) bound total storage but do nothing to prevent unprivileged, cheap population of that bounded store with garbage that will never resolve to a real `AttachmentInstance`. Once the store is full, `evict_k_oldest_uninstantiated_attachments` evicts oldest entries — which can include legitimate, still-unmatched attachments submitted by honest actors — before the corresponding `AttachmentInstance` (created later, e.g. after a slower client submits or during subsequent block processing) is ever checked against them.

### Impact Explanation
This is an unauthenticated write into node state (`AtlasDB`'s `attachments` table) not backed by any consensus commitment, causing churn/eviction of legitimate pending attachments that are genuinely tied to `AttachmentInstance` records from confirmed transactions. This matches the "High" category of "attachment/BNS mismatch" — the node's attachment cache can be made to no longer reflect legitimate, consensus-referenced attachment data due to unprivileged flooding, degrading BNS name-resolution attachment availability for the affected node. It does not crash the node or forge cross-network state, but it is a repeatable, low-cost griefing vector against local Atlas storage/availability.

### Likelihood Explanation
Preconditions are minimal and cheap for an attacker: the ability to submit standard `POST /v2/transactions` RPC requests (no secret/auth needed for this endpoint), a valid transaction that is accepted into the mempool (any well-formed `ContractCall` to an Atlas-tracked contract such as the BNS boot contract, with sufficient signature/nonce/fee to pass `mempool.submit`), and an attachment blob under `attachments_max_size` (minimum 1 MiB). The attacker can generate an unlimited number of distinct txids/attachments and repeat the request cheaply and remotely with no privileged role, mempool-fee cost aside. `max_uninstantiated_attachments` bounds total storage (default minimum 50,000) but does not prevent it from being filled entirely with attacker garbage.

### Recommendation
At attachment-store time in `posttransaction.rs`, before calling `insert_uninstantiated_attachment`, verify that `attachment.hash()` matches an existing (or at least plausible) `AttachmentInstance.content_hash` recorded in `AtlasDB` (e.g., query `find_all_attachment_instances`/instances for that hash, or require the instance to already be queued), rejecting attachments with no corresponding instance. Alternatively, defer storage of unmatched attachments to a separate, more aggressively bounded/short-TTL cache distinct from the one instances are matched against, so unmatched submissions cannot evict already-matched or genuinely pending legitimate attachments.

### Proof of Concept
Rust integration test plan (in `stackslib/src/net/atlas/tests.rs` or a new `posttransaction` test module):
1. Configure `AtlasConfig` with `contracts` containing the BNS boot contract id and `max_uninstantiated_attachments` set to a small test value (e.g., 5) for practicality.
2. Insert one legitimate `Attachment` `A_legit` with a hash matching a manually inserted `AttachmentInstance` (simulate a real pending BNS name-registration attachment) via `AtlasDB::insert_uninstantiated_attachment` and `insert_uninstantiated_attachment_instance`.
3. Drive `RPCPostTransactionRequestHandler::try_handle_request` (or call `should_keep_attachment` + `insert_uninstantiated_attachment` directly, mirroring the handler) N times (N = `max_uninstantiated_attachments`) with distinct signed `ContractCall` transactions to the BNS contract, each paired with a unique bogus `Attachment` whose hash matches no `AttachmentInstance`.
4. Assert `atlasdb.count_uninstantiated_attachments().unwrap() == max_uninstantiated_attachments` and that `A_legit` has been evicted by `evict_k_oldest_uninstantiated_attachments` (query for `A_legit.hash()` in the `attachments` table and assert it is absent), despite its corresponding `AttachmentInstance` never having been resolved. This demonstrates that unauthenticated garbage attachments displaced a legitimate, instance-backed attachment.

### Citations

**File:** stackslib/src/net/api/posttransaction.rs (L64-93)
```rust
    /// Decode a JSON-encoded transaction and Atlas attachment pair
    fn parse_posttransaction_json(
        body: &[u8],
    ) -> Result<(StacksTransaction, Option<Attachment>), Error> {
        let body: PostTransactionRequestBody = serde_json::from_slice(body)
            .map_err(|_e| Error::DecodeError("Failed to parse body".into()))?;

        let tx = {
            let tx_bytes = hex_bytes(&body.tx)
                .map_err(|_e| Error::DecodeError("Failed to parse tx".into()))?;
            StacksTransaction::consensus_deserialize(&mut &tx_bytes[..]).map_err(|e| {
                if let CodecError::DeserializeError(msg) = e {
                    Error::DecodeError(format!("Failed to deserialize posted transaction: {}", msg))
                } else {
                    e.into()
                }
            })
        }?;

        let attachment = match body.attachment {
            None => None,
            Some(ref attachment_content) => {
                let content = hex_bytes(attachment_content)
                    .map_err(|_e| Error::DecodeError("Failed to parse attachment".into()))?;
                Some(Attachment::new(content))
            }
        };

        Ok((tx, attachment))
    }
```

**File:** stackslib/src/net/api/posttransaction.rs (L230-251)
```rust
            // store attachment as well, if it's part of a contract-call
            if let Some(ref attachment) = attachment_opt {
                if let TransactionPayload::ContractCall(ref contract_call) = tx.payload {
                    if network
                        .get_atlasdb()
                        .should_keep_attachment(&contract_call.to_clarity_contract_id(), attachment)
                    {
                        network
                            .get_atlasdb_mut()
                            .insert_uninstantiated_attachment(attachment)
                            .map_err(|e| {
                                StacksHttpResponse::new_error(
                                    &preamble,
                                    &HttpServerError::new(format!(
                                        "Failed to store contract-call attachment: {:?}",
                                        &e
                                    )),
                                )
                            })?;
                    }
                }
            }
```

**File:** stackslib/src/net/atlas/mod.rs (L183-236)
```rust
impl AttachmentInstance {
    const ATTACHMENTS_INV_PAGE_SIZE: u32 = 64;

    pub fn try_new_from_value(
        value: &Value,
        contract_id: &QualifiedContractIdentifier,
        index_block_hash: StacksBlockId,
        stacks_block_height: u64,
        tx_id: Txid,
        canonical_stacks_tip_height: Option<u64>,
    ) -> Option<AttachmentInstance> {
        if let Value::Tuple(ref attachment) = value {
            if let Ok(Value::Tuple(ref attachment_data)) = attachment.get("attachment") {
                if let (
                    Ok(Value::Sequence(SequenceData::Buffer(content_hash))),
                    Ok(Value::UInt(attachment_index)),
                ) = (
                    attachment_data.get("hash"),
                    attachment_data.get("attachment-index"),
                ) {
                    let content_hash = if content_hash.data.is_empty() {
                        Hash160::empty()
                    } else {
                        match Hash160::from_bytes(&content_hash.data[..]) {
                            Some(content_hash) => content_hash,
                            _ => return None,
                        }
                    };
                    let metadata = match attachment_data.get("metadata") {
                        Ok(metadata) => {
                            let mut serialized = vec![];
                            metadata
                                .consensus_serialize(&mut serialized)
                                .expect("FATAL: invalid metadata");
                            to_hex(&serialized[..])
                        }
                        _ => String::new(),
                    };
                    let instance = AttachmentInstance {
                        index_block_hash,
                        content_hash,
                        attachment_index: *attachment_index as u32,
                        stacks_block_height,
                        metadata,
                        contract_id: contract_id.clone(),
                        tx_id,
                        canonical_stacks_tip_height,
                    };
                    return Some(instance);
                }
            }
        }
        None
    }
```
