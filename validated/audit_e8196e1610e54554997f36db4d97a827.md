### Title
Length-prefixed collection deserialization allocates before validating remaining bytes, allowing memory amplification via `/v2/fees/transaction` - ([File: stackslib/src/net/api/postfeerate.rs])

### Summary
`RPCPostFeeRateRequestHandler::try_parse_request` calls `TransactionPayload::consensus_deserialize` on fully attacker-controlled, hex-decoded bytes before any tip/cost check, gated only by the `content_len < MAX_PAYLOAD_LEN` body-size check. The generic `Vec<T>` codec used transitively by `TransactionPayload`'s variants (e.g. the contract-name/code-body byte strings inside `SmartContract`/`ContractCall`) calls `Vec::with_capacity(len as usize)` immediately after validating only that `size_of::<T>() * len <= MAX_MESSAGE_LEN`, without first checking how many bytes actually remain in the underlying reader.

### Finding Description
The declared-length vs. bytes-present equality is broken: `read_next_vec` in `stacks-common/src/codec/mod.rs` reads a `u32` length prefix, bounds it only against the global `MAX_MESSAGE_LEN` constant (~16MB), and then unconditionally does `Vec::with_capacity(len as usize)` before attempting to read any of the declared items: [1](#0-0) 

Because this check compares `size_of::<T>() * len` against `MAX_MESSAGE_LEN` rather than against the number of bytes actually remaining in the reader, a length-prefixed byte vector (e.g. a `Vec<u8>`/`StacksString`-style field used for contract code body inside `TransactionSmartContract`, which is decoded via `TransactionPayload::consensus_deserialize`) can declare a length close to `MAX_MESSAGE_LEN` while the attacker supplies only the few bytes needed to encode that length prefix. `Vec::with_capacity` will attempt to reserve up to ~16MB from a request body that itself can be only a few dozen bytes, well under the `MAX_PAYLOAD_LEN` gate at: [2](#0-1) 

The `TransactionPayload::consensus_deserialize` entry point demonstrates the reachable path — an unauthenticated `SmartContract`/`ContractCall`/`TokenTransfer` payload is decoded field-by-field via `read_next`, which for byte-string fields resolves to the generic `Vec<T>` implementation: [3](#0-2) 

The existing `MAX_PAYLOAD_LEN`/`MAX_MESSAGE_LEN` bounds cap the size of *any single allocation* to ~16MB, so this is not an unbounded allocation, but the check happens on the *declared* length, not on bytes actually available to satisfy it — so the allocation size is decoupled from the number of bytes the attacker had to send. A single POST of on the order of tens of bytes can force a ~16MB heap reservation before the subsequent per-item read loop fails with an `UnexpectedEof`.

### Impact Explanation
Each malicious request forces the node's RPC-handling thread to reserve on the order of `MAX_MESSAGE_LEN` (~16MB) bytes of heap from an unauthenticated, small HTTP POST, before any tip resolution or cost estimation occurs. This is repeatable per-connection/per-request with no state retained between requests, so an attacker can drive up memory allocator churn and CPU spent zeroing/reserving buffers using a small fraction of the bytes normally required to justify that allocation size — i.e., a bytes-sent-vs-memory-allocated amplification on a publicly reachable, unauthenticated RPC endpoint. This does not, by itself, demonstrate an unbounded or catastrophic single-message crash, since the allocation is capped at `MAX_MESSAGE_LEN` per field (not attacker-arbitrary), which is a pre-existing global bound applied throughout the codec, not something unique to `postfeerate.rs`.

### Likelihood Explanation
No preconditions beyond TCP reachability to the node's RPC port are required; the endpoint requires no auth token, peer key, or StackerDB slot. Attacker cost is minimal (a small JSON POST with a short hex string). The behavior is deterministic and repeatable.

### Recommendation
In `read_next_vec` (`stacks-common/src/codec/mod.rs`), avoid reserving capacity based purely on the untrusted declared length. Either bound the reservation using a conservative fixed cap independent of the wire-declared length (e.g. reserve incrementally, growing the `Vec` as items are actually successfully read, rather than calling `Vec::with_capacity(len)` up front), or require the reader to expose/enforce a remaining-bytes bound (already available via `BoundReader`, as used elsewhere e.g. in `OrderIndependentMultisigSpendingCondition::consensus_deserialize`) so the capacity reservation cannot exceed the bytes actually available to the deserializer.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postfeerate.rs` (or a codec-level test) that:
1. Crafts a `TransactionPayload::SmartContract` wire encoding where the code-body/name length field is set near `MAX_MESSAGE_LEN` but only a handful of trailing bytes are actually supplied.
2. Hex-encodes this short byte sequence into a `FeeRateEstimateRequestBody.transaction_payload` JSON body well under `MAX_PAYLOAD_LEN`.
3. Invokes `RPCPostFeeRateRequestHandler::try_parse_request` with this body and observes (via a custom allocator counter, or `valgrind`/`heaptrack`) that `Vec::with_capacity` reserves close to `MAX_MESSAGE_LEN` bytes despite the tiny request size, before returning an `UnexpectedEof`/`DeserializeError`.
4. Assert that the fix caps the reservation to the number of bytes actually available in `payload_data`, not the declared length.

### Citations

**File:** stacks-common/src/codec/mod.rs (L153-190)
```rust
fn read_next_vec<T: StacksMessageCodec + Sized, R: Read>(
    fd: &mut R,
    num_items: u32,
    max_items: u32,
) -> Result<Vec<T>, Error> {
    let len = u32::consensus_deserialize(fd)?;

    if max_items > 0 {
        if len > max_items {
            // too many items
            return Err(Error::DeserializeError(format!(
                "Array has too many items ({len} > {max_items})"
            )));
        }
    } else if len != num_items {
        // inexact item count
        return Err(Error::DeserializeError(format!(
            "Array has incorrect number of items ({len} != {num_items})"
        )));
    }

    if (mem::size_of::<T>() as u128) * (len as u128) > MAX_MESSAGE_LEN as u128 {
        return Err(Error::DeserializeError(format!(
            "Message occupies too many bytes (tried to allocate {}*{}={})",
            mem::size_of::<T>() as u128,
            len,
            (mem::size_of::<T>() as u128) * (len as u128)
        )));
    }

    let mut ret = Vec::with_capacity(len as usize);
    for _i in 0..len {
        let next_item = T::consensus_deserialize(fd)?;
        ret.push(next_item);
    }

    Ok(ret)
}
```

**File:** stackslib/src/net/api/postfeerate.rs (L150-179)
```rust
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        let content_len = preamble.get_content_length();
        if !(content_len > 0 && content_len < MAX_PAYLOAD_LEN) {
            return Err(Error::DecodeError(format!(
                "Invalid Http request: invalid body length for FeeRateEstimate ({})",
                content_len
            )));
        }

        if preamble.content_type != Some(HttpContentType::JSON) {
            return Err(Error::DecodeError(
                "Invalid content-type: expected application/json".to_string(),
            ));
        }

        let body: FeeRateEstimateRequestBody = serde_json::from_slice(body)
            .map_err(|e| Error::DecodeError(format!("Failed to parse JSON body: {}", e)))?;

        let payload_hex = if body.transaction_payload.starts_with("0x") {
            &body.transaction_payload[2..]
        } else {
            &body.transaction_payload
        };

        let payload_data = hex_bytes(payload_hex).map_err(|_e| {
            Error::DecodeError("Bad hex string supplied for transaction payload".into())
        })?;

        let tx = TransactionPayload::consensus_deserialize(&mut payload_data.as_slice())?;
```

**File:** stacks-codec/src/transaction.rs (L2878-2904)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<TransactionPayload, codec_error> {
        let type_id_u8 = read_next(fd)?;
        let type_id = TransactionPayloadID::from_u8(type_id_u8).ok_or_else(|| {
            codec_error::DeserializeError(format!(
                "Failed to parse transaction -- unknown payload ID {type_id_u8}"
            ))
        })?;
        let payload = match type_id {
            TransactionPayloadID::TokenTransfer => {
                let principal = read_next(fd)?;
                let amount = read_next(fd)?;
                let memo = read_next(fd)?;
                TransactionPayload::TokenTransfer(principal, amount, memo)
            }
            TransactionPayloadID::ContractCall => {
                let payload: TransactionContractCall = read_next(fd)?;
                TransactionPayload::ContractCall(payload)
            }
            TransactionPayloadID::SmartContract => {
                let payload: TransactionSmartContract = read_next(fd)?;
                TransactionPayload::SmartContract(payload, None)
            }
            TransactionPayloadID::VersionedSmartContract => {
                let version = clarity_version_consensus_deserialize(fd)?;
                let payload: TransactionSmartContract = read_next(fd)?;
                TransactionPayload::SmartContract(payload, Some(version))
            }
```
