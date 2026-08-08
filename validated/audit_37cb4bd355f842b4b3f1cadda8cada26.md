### No vulnerability found for this question.

The premise doesn't hold against the actual code: `verify_packet` is `#[must_use]` [1](#0-0)  and both real call sites correctly discard the packet whenever it returns `false`, regardless of *why* it returned false (whether via `signatures.is_empty()` short-circuit, `try_new_sanitized` failure, or otherwise): [2](#0-1) [3](#0-2) 

In both `ed25519_verify` and `ed25519_verify_serial`, the pattern is `if !packet.meta().discard() && !verify_packet(...) { packet.meta_mut().set_discard(true); }` — the boolean returned by `verify_packet` is always consumed and applied before `count_valid_packets` is invoked. There is no caller in the codebase (checked via `grep_search` for `verify_packet` usages in `perf/src/sigverify.rs`) that ignores the return value or calls `verify_packet` without following up with `set_discard`. The unit tests (`test_pubkey_too_small`, `test_pubkey_len`, etc.) explicitly confirm that after `ed25519_verify`, packets whose `verify_packet` returns `false` end up with `discard() == true` [4](#0-3) .

Since the short-circuit paths inside `verify_packet` (empty signatures, sanitize failure, unsupported version, etc.) all correctly return `false`, and every real caller in the repo applies that `false` to `set_discard(true)` before `count_valid_packets` runs [5](#0-4) , there is no reachable code path where an attacker-controlled packet with a failing/empty signature check ends up counted as valid. The scenario requires a hypothetical caller that "forgets" to apply the returned bool, which does not exist in this codebase, so this is not an exploitable finding under the current implementation.

### Citations

**File:** perf/src/sigverify.rs (L17-20)
```rust
/// Returns true if the signature on the packet verifies.
/// Caller must do packet.set_discard(true) if this returns false.
#[must_use]
fn verify_packet(packet: &mut PacketRefMut, reject_non_vote: bool, enable_tx_v1: bool) -> bool {
```

**File:** perf/src/sigverify.rs (L69-74)
```rust
pub fn count_valid_packets<'a>(batches: impl IntoIterator<Item = &'a PacketBatch>) -> usize {
    batches
        .into_iter()
        .map(|batch| batch.into_iter().filter(|p| !p.meta().discard()).count())
        .sum()
}
```

**File:** perf/src/sigverify.rs (L117-124)
```rust
        batches.par_iter_mut().flatten().for_each(|mut packet| {
            if !packet.meta().discard()
                && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1)
            {
                packet.meta_mut().set_discard(true);
            }
        });
    });
```

**File:** perf/src/sigverify.rs (L127-133)
```rust
pub fn ed25519_verify_serial(batch: &mut PacketBatch, reject_non_vote: bool, enable_tx_v1: bool) {
    for mut packet in batch.iter_mut() {
        if !packet.meta().discard() && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1) {
            packet.meta_mut().set_discard(true);
        }
    }
}
```

**File:** perf/src/sigverify.rs (L291-299)
```rust
        let mut packet = BytesPacket::from_data(tx).unwrap();

        assert!(!verify_packet(&mut packet.as_mut(), false, false));

        packet.meta_mut().set_discard(false);
        let mut batches = generate_packet_batches(&packet, 1, 1);
        ed25519_verify(&mut batches);
        assert!(batches[0].get(0).unwrap().meta().discard());
    }
```
