### Title
Allocation Amplification via Missing Item-Count Cap in `MicroblocksData` Deserialization - (File: stackslib/src/net/codec.rs)

### Summary
`MicroblocksData::consensus_deserialize` deserializes its `microblocks: Vec<StacksMicroblock>` field via the generic `Vec<T>` codec path, which enforces no real cap on the claimed element count (`max_items = u32::MAX`). This mirrors the Zebra bug class: a count field is trusted to preallocate memory before the real protocol/consensus-level limit is enforced. Sibling message types (`NakamotoBlocksData`, `BlocksData`) explicitly guard against this by passing a tight `max_items` constant into `read_next_at_most`, but `MicroblocksData` does not.

### Finding Description
`stacks_common::codec::read_next_vec` (used by all `Vec<T>` deserialization) takes a `max_items` parameter and, when `max_items > 0`, rejects any claimed length greater than that cap before allocating: [1](#0-0) 

The default `Vec<T>: StacksMessageCodec::consensus_deserialize` implementation calls `read_next_at_most::<R, T>(fd, u32::MAX)`, meaning the `len > max_items` branch can never trigger (`len` is a `u32`, so it can never exceed `u32::MAX`): [2](#0-1) 

The only remaining guard is the byte-size sanity check `size_of::<T>() * len > MAX_MESSAGE_LEN`, computed against the **in-memory stack size** of `T`, not its actual wire size. For a struct like `StacksMicroblock` that is dominated by heap-backed `Vec` fields, `size_of::<T>()` is small and constant, so this check permits a very large `len` (on the order of `MAX_MESSAGE_LEN / size_of::<StacksMicroblock>()`) — at which point `Vec::with_capacity(len)` preallocates capacity for that many `StacksMicroblock` slots before a single item is actually parsed from the wire: [3](#0-2) 

Contrast this with the sibling relay message types in the same file, which explicitly comment on the "loose upper-bound" byte reader and additionally pass a real, tight item-count cap into `read_next_at_most` (`NAKAMOTO_BLOCKS_PUSHED_MAX`, `BLOCKS_PUSHED_MAX`) so the length field itself is bounded to a sane count before any allocation of `Vec::with_capacity`: [4](#0-3) [5](#0-4) 

`MicroblocksData` is missing this equivalent tight cap — it relies solely on the generic `Vec<T>` path's ineffective `u32::MAX` item bound plus the memory-size heuristic, which is exactly the "allocate against the loose ceiling before the tighter rule is enforced" pattern described in the report.

### Impact Explanation
An unauthenticated-but-handshaked peer sending a `Microblocks` P2P message with an inflated 4-byte length prefix for the `microblocks` vector can force the receiving node to call `Vec::with_capacity` for a large number of `StacksMicroblock` slots before any of the corresponding microblock bytes have actually been supplied or validated, amplifying allocation cost relative to the bytes the attacker had to send. This is a Denial-of-Service class issue (excess memory/parse-cost amplification per message), consistent with the "Medium" severity classification in the referenced Zebra advisory, and is stackable across concurrent connections/messages.

### Likelihood Explanation
The message field is reachable after the P2P handshake (no special privilege needed) and requires only a single crafted `Microblocks` message; no consensus-level state is required to trigger the vector-length parsing path. Likelihood is bounded by the same practical limits mentioned in the report (transport/message-size ceilings limit the worst case), but the specific missing-cap gap on `MicroblocksData` is a straightforward, low-effort trigger.

### Recommendation
Add an explicit, protocol-appropriate maximum item count for the `microblocks` vector (analogous to `NAKAMOTO_BLOCKS_PUSHED_MAX` / `BLOCKS_PUSHED_MAX`) and deserialize via `read_next_at_most(&mut bound_read, MAX_MICROBLOCKS_PER_MESSAGE)` instead of the generic `read_next(&mut bound_read)` for `Vec<StacksMicroblock>`, so that the length field is checked against a tight cap before any `Vec::with_capacity` allocation occurs.

### Proof of Concept
1. Complete a P2P handshake with a target node.
2. Send a `Microblocks` message whose payload declares an `index_anchor_block`, followed by a `u32` length prefix for `microblocks` set to a large value (bounded only by `MAX_MESSAGE_LEN / size_of::<StacksMicroblock>()`, since `read_next_vec`'s `len > max_items` check is unreachable for `Vec<T>`'s default path).
3. Truncate or omit the actual microblock payload bytes.
4. Observe that `MicroblocksData::consensus_deserialize` (`stackslib/src/net/codec.rs:546-552`) triggers `Vec::with_capacity(len)` for the declared count before reading/validating any microblock content, before ultimately failing on read due to `BoundReader` exhaustion — the preallocation cost has already been paid.

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

**File:** stacks-common/src/codec/mod.rs (L192-240)
```rust
pub fn read_next_at_most<R: Read, T: StacksMessageCodec + Sized>(
    fd: &mut R,
    max_items: u32,
) -> Result<Vec<T>, Error> {
    read_next_vec::<T, R>(fd, 0, max_items)
}

pub fn read_next_exact<R: Read, T: StacksMessageCodec + Sized>(
    fd: &mut R,
    num_items: u32,
) -> Result<Vec<T>, Error> {
    read_next_vec::<T, R>(fd, num_items, 0)
}

impl<A, B> StacksMessageCodec for (A, B)
where
    A: StacksMessageCodec + Sized,
    B: StacksMessageCodec + Sized,
{
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), Error> {
        write_next(fd, &self.0)?;
        write_next(fd, &self.1)?;
        Ok(())
    }

    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<(A, B), Error> {
        let a: A = read_next(fd)?;
        let b: B = read_next(fd)?;
        Ok((a, b))
    }
}

impl<T> StacksMessageCodec for Vec<T>
where
    T: StacksMessageCodec + Sized,
{
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), Error> {
        let len = self.len() as u32;
        write_next(fd, &len)?;
        for item in self {
            write_next(fd, item)?;
        }
        Ok(())
    }

    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<Vec<T>, Error> {
        read_next_at_most::<R, T>(fd, u32::MAX)
    }
}
```

**File:** stackslib/src/net/codec.rs (L358-387)
```rust
impl StacksMessageCodec for NakamotoBlocksData {
    #[cfg_attr(test, mutants::skip)]
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), codec_error> {
        write_next(fd, &self.blocks)?;
        Ok(())
    }

    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<Self, codec_error> {
        let blocks: Vec<NakamotoBlock> = {
            // loose upper-bound
            let mut bound_read = BoundReader::from_reader(fd, MAX_MESSAGE_LEN as u64);
            read_next_at_most::<_, NakamotoBlock>(&mut bound_read, NAKAMOTO_BLOCKS_PUSHED_MAX)
        }?;

        // only valid if there are no dups
        let mut present = HashSet::new();
        for block in blocks.iter() {
            if present.contains(&block.block_id()) {
                // no dups allowed
                return Err(codec_error::DeserializeError(
                    "Invalid NakamotoBlocksData: duplicate block".to_string(),
                ));
            }

            present.insert(block.block_id());
        }

        Ok(NakamotoBlocksData { blocks })
    }
}
```

**File:** stackslib/src/net/codec.rs (L509-537)
```rust
impl StacksMessageCodec for BlocksData {
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), codec_error> {
        write_next(fd, &self.blocks)?;
        Ok(())
    }

    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<BlocksData, codec_error> {
        let blocks: Vec<BlocksDatum> = {
            // loose upper-bound
            let mut bound_read = BoundReader::from_reader(fd, MAX_MESSAGE_LEN as u64);
            read_next_at_most::<_, BlocksDatum>(&mut bound_read, BLOCKS_PUSHED_MAX)
        }?;

        // only valid if there are no dups
        let mut present = HashSet::new();
        for BlocksDatum(consensus_hash, _block) in blocks.iter() {
            if present.contains(consensus_hash) {
                // no dups allowed
                return Err(codec_error::DeserializeError(
                    "Invalid BlocksData: duplicate block".to_string(),
                ));
            }

            present.insert(consensus_hash.clone());
        }

        Ok(BlocksData { blocks })
    }
}
```
