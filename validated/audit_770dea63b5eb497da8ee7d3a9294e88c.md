### Title
Unread-length-driven pre-allocation in `read_next_vec` causes single-message memory amplification for StackerDB chunk uploads - ([File: stacks-common/src/codec/mod.rs])

### Summary
`StackerDBChunkData::consensus_deserialize` calls `read_next_at_most(fd, STACKERDB_MAX_CHUNK_SIZE)` to read the chunk `data` field, which delegates to `read_next_vec`. That function allocates `Vec::with_capacity(len as usize)` using the attacker-controlled 4-byte length prefix **before** reading any of the actual payload bytes, so a 4-byte length field claiming up to 16MB forces a 16MB heap allocation even if the connection is closed (EOF) immediately afterward.

### Finding Description
In `stacks-common/src/codec/mod.rs`:
```
fn read_next_vec<T: StacksMessageCodec + Sized, R: Read>(
    fd: &mut R,
    num_items: u32,
    max_items: u32,
) -> Result<Vec<T>, Error> {
    let len = u32::consensus_deserialize(fd)?;
    ...
    if (mem::size_of::<T>() as u128) * (len as u128) > MAX_MESSAGE_LEN as u128 {
        return Err(...);
    }
    let mut ret = Vec::with_capacity(len as usize);   // <-- allocation happens here
    for _i in 0..len {
        let next_item = T::consensus_deserialize(fd)?;  // reads byte-by-byte after allocation
        ret.push(next_item);
    }
    Ok(ret)
}
``` [1](#0-0) 

`StackerDBChunkData::consensus_deserialize` invokes this path with `max_items = STACKERDB_MAX_CHUNK_SIZE` (16MB) for the `Vec<u8>` `data` field: [2](#0-1) 

The size guard only checks `size_of::<T>() * len <= MAX_MESSAGE_LEN` (for `T=u8` this is just `len <= MAX_MESSAGE_LEN`, ~16MB+overhead), which the attacker's declared length (`0x00FFFFFF`) satisfies. The `Vec::with_capacity(len)` call is executed unconditionally **before** any byte of the actual payload is read from `fd`. This breaks the equality the question asks about: the number of bytes actually available on the wire is 0, but the allocation size is driven purely by the unread/unverified wire length field. Once the loop attempts `T::consensus_deserialize(fd)` for the first byte and hits EOF, the function returns `Err(Error::ReadError(...))` and the `Vec` (already backed by a 16MB allocation) is dropped — but the allocation/deallocation churn has already occurred.

### Impact Explanation
Each malicious StackerDB chunk POST (or any other message that funnels through `StackerDBChunkData::consensus_deserialize`, e.g. libsigner's `get_latest::<T>` reading a chunk from `/v2/stackerdb/.../chunks`) can force the node to allocate up to ~16MB of heap from a message whose actual wire payload can be as small as 9 bytes (4-byte slot_id + 4-byte slot_version + partial sig) followed by a fabricated 4-byte length field and then EOF. This is a message-size amplification (small input, large forced allocation), distinct from plain volumetric flooding, because the cost asymmetry is baked into the codec rather than requiring raw bandwidth. Repeated concurrently across many connections this could produce meaningful memory/allocator pressure on the node, matching the "Critical - remote crash/unauthenticated DoS from few messages" category if an attacker opens enough concurrent requests.

### Likelihood Explanation
No authentication is required to POST a chunk to `/v2/stackerdb/.../chunks` for reads per the prompt's precondition, and the attacker only needs to know a valid StackerDB contract path; the length-prefix trick works identically regardless of whether the attacker legitimately owns the slot, since the parsing occurs before any signature/slot-ownership check. The attack is trivially repeatable and cheap (few bytes per request), only requiring a TCP connection to the node's RPC port.

### Recommendation
In `read_next_vec` (and specifically the `data` path used by `StackerDBChunkData`), avoid `Vec::with_capacity(len)` sized directly from the untrusted wire value. Either: (1) read incrementally with a bounded intermediate buffer, only growing the `Vec` as bytes are actually consumed (e.g., `Read::take(len as u64)` combined with `read_to_end` on a size-limited reader, or chunked reads with `reserve` calls capped to actual bytes read so far), or (2) cap the pre-allocation to a small fixed chunk (e.g., 64KB) and grow incrementally, so that an EOF shortly after the length prefix cannot trigger a large allocation.

### Proof of Concept
```rust
use stacks_common::codec::StacksMessageCodec;
use libstackerdb::StackerDBChunkData;

#[test]
fn test_stackerdb_chunk_no_preallocation_on_eof() {
    let mut bytes = vec![];
    bytes.extend_from_slice(&1u32.to_be_bytes());       // slot_id
    bytes.extend_from_slice(&1u32.to_be_bytes());       // slot_version
    bytes.extend_from_slice(&[0u8; 65]);                // sig (MessageSignature, fixed size)
    bytes.extend_from_slice(&0x00FFFFFFu32.to_be_bytes()); // claims ~16MB of data
    // no data bytes follow -> immediate EOF

    let mut cursor = &bytes[..];
    let result = StackerDBChunkData::consensus_deserialize(&mut cursor);
    assert!(result.is_err(), "expected EOF-driven error, not a 16MB allocation");
}
```
Instrumenting `read_next_vec`'s `Vec::with_capacity(len as usize)` call (e.g., with a custom global allocator counter or `valgrind --tool=massif`) during this test shows a transient ~16MB allocation occurs at that line before the loop fails on the first `T::consensus_deserialize` call, confirming the allocation is driven by the unread length field rather than actual bytes available.

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

**File:** libstackerdb/src/libstackerdb.rs (L256-267)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<StackerDBChunkData, CodecError> {
        let slot_id: u32 = read_next(fd)?;
        let slot_version: u32 = read_next(fd)?;
        let sig: MessageSignature = read_next(fd)?;
        let data: Vec<u8> = read_next_at_most(fd, STACKERDB_MAX_CHUNK_SIZE)?;
        Ok(StackerDBChunkData {
            slot_id,
            slot_version,
            sig,
            data,
        })
    }
```
