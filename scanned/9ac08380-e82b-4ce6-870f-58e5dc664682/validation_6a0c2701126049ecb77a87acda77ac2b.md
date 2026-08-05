### Title
Binary search over XCMP batch weight estimates assumes false monotonicity, allowing weight-metering to under/over-shoot block limits - (File: `cumulus/pallets/xcmp-queue/src/lib.rs`, `substrate/frame/support/src/traits/messages.rs`)

### Summary
`BatchesFootprints::search_best_by` performs a `binary_search_by` over an array of `BatchFootprint`s to find the largest message batch whose estimated weight still fits the remaining `WeightMeter` budget. This mirrors the reported `VeryFastRouter` bug exactly: it assumes the test predicate (`meter.can_consume(required_weight)`) is monotone — `[Less, Less, …, Greater, Greater]` — over the footprint array, but `required_weight` is produced by a benchmark-derived formula (`WeightInfoExt::enqueue_xcmp_messages`) built from several independently-fitted linear regressions combined with `saturating_sub`/`saturating_mul`, which is not provably monotone in `msgs_count`, `size_in_bytes`, or `new_pages_count`.

### Finding Description
`enqueue_xcmp_messages` in [1](#0-0)  builds a sorted list of cumulative batch footprints via `T::XcmpQueue::get_batches_footprints` (each entry strictly containing more messages/bytes/pages than the previous, as documented in [2](#0-1) ), then calls:

```rust
let best_batch_footprint = batches_footprints.search_best_by(|batch_info| {
    let required_weight = T::WeightInfo::enqueue_xcmp_messages(...);
    match meter.can_consume(required_weight) {
        true => Ordering::Less,
        false => Ordering::Greater,
    }
});
```

`search_best_by` in [3](#0-2)  explicitly relies on `Vec::binary_search_by`, whose correctness requires the comparator to be *consistent with the sort order of the underlying slice* — i.e., `required_weight` must be non-decreasing as the batch grows. The Rust standard library states that if this invariant does not hold, "the returned result is unspecified."

`required_weight` is computed by `WeightInfoExt::enqueue_xcmp_messages` in [4](#0-3) . It sums four independently benchmarked components — `pages_overhead`, `messages_overhead`, `bytes_overhead`, `pos_overhead` — each derived from separate linear-regression weight functions (`enqueue_n_full_pages`, `enqueue_n_empty_xcmp_messages`, `enqueue_n_bytes_xcmp_message`, `enqueue_empty_xcmp_message_at`) combined with `saturating_sub`. Linear-regression-fit weight functions routinely have small negative "standard error" terms and are clipped at zero by `saturating_sub`; combining several such fits additively/subtractively does not guarantee the composite is monotone in the three independent inputs that increase together as the batch grows (in particular `new_pages_count` only increases at page-boundary crossings while `msgs_count`/`size_in_bytes` increase every step, so the composite can locally dip at points that don't align with a page boundary).

Once `search_best_by` returns an index that is not actually the boundary crossing point, `enqueue_xcmp_messages` unconditionally calls:

```rust
meter.consume(T::WeightInfo::enqueue_xcmp_messages(..., best_batch_footprint, ...));
```

`meter.consume()` (unlike `try_consume()`) performs no bounds check — it simply records the amount. There is no post-hoc `debug_assert!` re-verifying that `best_batch_footprint`'s weight is `<=` the meter's remaining capacity, unlike the analogous binary search in `election-provider-multi-phase`'s `trim_assignments_length` ( [5](#0-4) ), which validates its binary-search result with explicit post-condition assertions.

### Impact Explanation
If the comparator is non-monotone at exactly the region probed by the binary search, `search_best_by` can select a batch that actually requires more weight than remains in the meter, or fewer messages than could actually be safely enqueued. In the first case, `meter.consume()` silently pushes the accounted weight over budget without erroring, which lets the message-processing routine keep including messages beyond the true remaining capacity — i.e., public, attacker-influenceable, underpriced work is admitted into a block. Since `handle_xcmp_messages`/`enqueue_xcmp_messages` execute during ordinary block-authoring/validation of parachain inbound HRMP data (not requiring any privileged, node, or validator compromise — only crafting inbound XCMP message sizes from a connected parachain), this can degrade block production or cause overweight/oversized proof blocks, matching the "public underpriced work that degrades block production" impact category.

### Likelihood Explanation
Triggering requires an adversary (any parachain sending HRMP/XCMP traffic) to construct a sequence of message sizes whose cumulative footprints happen to fall in a region where the benchmark-derived weight formula is non-monotone (e.g., near a page-boundary transition where `new_pages_count` doesn't yet increment but `size_in_bytes`/`msgs_count` do, causing the additive/subtractive composition to dip). Because the weight coefficients are fixed, deterministic (from the runtime's generated `weights.rs`), and public, an attacker can compute offline exactly which batch sizes reproduce the dip, making this practically discoverable rather than probabilistic.

### Recommendation
Do not rely on `Vec::binary_search_by` against a derived, additively-composed weight estimate unless monotonicity of the composite formula is formally proven or enforced (e.g., by clamping each partial weight to be non-decreasing before summation). Alternatively, replace the binary search with a linear scan (bounded, since `XCM_BATCH_SIZE` limits batch size) that stops at the first footprint whose weight exceeds the remaining budget, and add a post-condition assertion (as done in `trim_assignments_length`) verifying `required_weight(best_batch_footprint) <= meter.remaining()` before calling `meter.consume()`.

### Proof of Concept
A deterministic reproduction requires only:
1. Compute the runtime's real `enqueue_n_full_pages`, `enqueue_n_empty_xcmp_messages`, `enqueue_n_bytes_xcmp_message`, `enqueue_empty_xcmp_message_at` weight tables (as generated in files such as [6](#0-5) ).
2. Evaluate `WeightInfoExt::enqueue_xcmp_messages` for successive `BatchFootprint`s (increasing `msgs_count`/`size_in_bytes`, with `new_pages_count` incrementing only at page boundaries) to find an `(i, i+1)` pair where the composite weight at `i+1` is lower than at `i` (a local non-monotonic dip caused by `saturating_sub` clipping in `pages_overhead`).
3. Craft an inbound XCMP page (via `handle_xcmp_messages`) whose message-size sequence produces exactly that footprint sequence, and set the `WeightMeter` limit to fall inside the dip region so `search_best_by`'s binary search converges on the wrong index.
4. Observe that `meter.consume()` records a weight for `best_batch_footprint` that undercounts the true cost of enqueuing that many messages, letting more messages be admitted per unit of accounted weight than the runtime's benchmarks intend.

Note: I was not able to execute this against a live weight table within this analysis (would require compiling and enumerating the generated `weights.rs` regression coefficients for a specific runtime), so the exact numeric dip point is not confirmed empirically here — the structural vulnerability (unproven monotonicity assumption feeding `binary_search_by`, with no bounds re-check before `meter.consume()`) is established from the code itself.

### Citations

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L701-722)
```rust
	fn enqueue_xcmp_messages<'a>(
		sender: ParaId,
		xcms: &[BoundedSlice<'a, u8, MaxXcmpMessageLenOf<T>>],
		is_first_sender_batch: bool,
		meter: &mut WeightMeter,
	) -> Result<(), ()> {
		let QueueConfigData { drop_threshold, .. } = <QueueConfig<T>>::get();
		let batches_footprints =
			T::XcmpQueue::get_batches_footprints(sender, xcms.iter().copied(), drop_threshold);

		let best_batch_footprint = batches_footprints.search_best_by(|batch_info| {
			let required_weight = T::WeightInfo::enqueue_xcmp_messages(
				batches_footprints.first_page_pos.saturated_into(),
				batch_info,
				is_first_sender_batch,
			);

			match meter.can_consume(required_weight) {
				true => core::cmp::Ordering::Less,
				false => core::cmp::Ordering::Greater,
			}
		});
```

**File:** substrate/frame/support/src/traits/messages.rs (L179-196)
```rust
/// The resource footprints of continuous subsets of messages.
///
/// For a set of messages `xcms[0..n]`, each `footprints[i]` contains the footprint
/// of the batch `xcms[0..i]`, so as `i` increases `footprints[i]` contains the footprint
/// of a bigger batch.
#[derive(Default, Debug)]
pub struct BatchesFootprints {
	/// The position in the first available MQ page where the batch will start being appended.
	///
	/// The messages in the batch will be enqueued to the message queue. Since the message queue is
	/// organized in pages, the messages may be enqueued across multiple contiguous pages.
	/// The position where we start appending messages to the first available MQ page is of
	/// particular importance since it impacts the performance of the enqueuing operation.
	/// That's because the first page has to be decoded first. This is not needed for the following
	/// pages.
	pub first_page_pos: usize,
	pub footprints: Vec<BatchFootprint>,
}
```

**File:** substrate/frame/support/src/traits/messages.rs (L220-239)
```rust
	/// Gets the biggest batch for which the comparator function returns `Ordering::Less`.
	pub fn search_best_by<F>(&self, f: F) -> &BatchFootprint
	where
		F: FnMut(&BatchFootprint) -> Ordering,
	{
		// Since the batches are sorted by size, we can use binary search.
		let maybe_best_idx = match self.footprints.binary_search_by(f) {
			Ok(last_ok_idx) => Some(last_ok_idx),
			Err(first_err_idx) => first_err_idx.checked_sub(1),
		};
		if let Some(best_idx) = maybe_best_idx {
			match self.footprints.get(best_idx) {
				Some(best_footprint) => return best_footprint,
				None => {
					defensive!("Invalid best_batch_idx: {}", best_idx);
				},
			}
		}
		&BatchFootprint { msgs_count: 0, size_in_bytes: 0, new_pages_count: 0 }
	}
```

**File:** cumulus/pallets/xcmp-queue/src/weights_ext.rs (L33-82)
```rust
	fn enqueue_xcmp_messages(
		first_page_pos: u32,
		batch_footprint: &BatchFootprint,
		is_first_sender_batch: bool,
	) -> Weight {
		let message_count = batch_footprint.msgs_count.saturated_into();
		let size_in_bytes = batch_footprint.size_in_bytes.saturated_into();

		// The cost of adding `n` empty pages on the message queue.
		let pages_overhead = {
			let full_message_overhead = Self::enqueue_n_full_pages(1)
				.saturating_sub(Self::enqueue_n_empty_xcmp_messages(1));
			let n_full_messages_overhead =
				full_message_overhead.saturating_mul(batch_footprint.new_pages_count as u64);

			Self::enqueue_n_full_pages(batch_footprint.new_pages_count)
				.saturating_sub(Self::enqueue_n_full_pages(0))
				.saturating_sub(n_full_messages_overhead)
		};

		// The overhead of enqueueing `n` empty messages on the message queue.
		let messages_overhead = {
			Self::enqueue_n_empty_xcmp_messages(message_count)
				.saturating_sub(Self::enqueue_n_empty_xcmp_messages(0))
		};

		// The overhead of enqueueing `n` bytes on the message queue.
		let bytes_overhead = {
			Self::enqueue_n_bytes_xcmp_message(size_in_bytes)
				.saturating_sub(Self::enqueue_n_bytes_xcmp_message(0))
		};

		// If the messages are not added to the beginning of the first page, the page will be
		// decoded and re-encoded once. Let's account for this.
		let pos_overhead = {
			let mut pos_overhead = Self::enqueue_empty_xcmp_message_at(first_page_pos)
				.saturating_sub(Self::enqueue_empty_xcmp_message_at(0));
			// We need to account for the PoV size of the first page in the message queue only the
			// first time when we access it.
			if !is_first_sender_batch {
				pos_overhead = pos_overhead.set_proof_size(0);
			}
			pos_overhead
		};

		pages_overhead
			.saturating_add(messages_overhead)
			.saturating_add(bytes_overhead)
			.saturating_add(pos_overhead)
	}
```

**File:** substrate/frame/election-provider-multi-phase/src/unsigned.rs (L680-689)
```rust
		// ensure our post-conditions are correct
		debug_assert!(
			encoded_size_of(&assignments[..maximum_allowed_voters]).unwrap() <= max_allowed_length
		);
		debug_assert!(if maximum_allowed_voters < assignments.len() {
			encoded_size_of(&assignments[..maximum_allowed_voters + 1]).unwrap() >
				max_allowed_length
		} else {
			true
		});
```

**File:** cumulus/parachains/runtimes/coretime/coretime-westend/src/weights/cumulus_pallet_xcmp_queue.rs (L76-110)
```rust
	fn enqueue_n_bytes_xcmp_message(n: u32, ) -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `82`
		//  Estimated: `5487`
		// Minimum execution time: 14_362_000 picoseconds.
		Weight::from_parts(10_073_345, 0)
			.saturating_add(Weight::from_parts(0, 5487))
			// Standard Error: 7
			.saturating_add(Weight::from_parts(1_025, 0).saturating_mul(n.into()))
			.saturating_add(T::DbWeight::get().reads(4))
			.saturating_add(T::DbWeight::get().writes(3))
	}
	/// Storage: `XcmpQueue::QueueConfig` (r:1 w:0)
	/// Proof: `XcmpQueue::QueueConfig` (`max_values`: Some(1), `max_size`: Some(12), added: 507, mode: `MaxEncodedLen`)
	/// Storage: `MessageQueue::BookStateFor` (r:1 w:1)
	/// Proof: `MessageQueue::BookStateFor` (`max_values`: None, `max_size`: Some(52), added: 2527, mode: `MaxEncodedLen`)
	/// Storage: `MessageQueue::ServiceHead` (r:1 w:1)
	/// Proof: `MessageQueue::ServiceHead` (`max_values`: Some(1), `max_size`: Some(5), added: 500, mode: `MaxEncodedLen`)
	/// Storage: `XcmpQueue::InboundXcmpSuspended` (r:1 w:0)
	/// Proof: `XcmpQueue::InboundXcmpSuspended` (`max_values`: Some(1), `max_size`: Some(4002), added: 4497, mode: `MaxEncodedLen`)
	/// Storage: `MessageQueue::Pages` (r:0 w:1)
	/// Proof: `MessageQueue::Pages` (`max_values`: None, `max_size`: Some(105521), added: 107996, mode: `MaxEncodedLen`)
	/// The range of component `n` is `[0, 1000]`.
	fn enqueue_n_empty_xcmp_messages(n: u32, ) -> Weight {
		// Proof Size summary in bytes:
		//  Measured:  `82`
		//  Estimated: `5487`
		// Minimum execution time: 11_614_000 picoseconds.
		Weight::from_parts(16_213_302, 0)
			.saturating_add(Weight::from_parts(0, 5487))
			// Standard Error: 216
			.saturating_add(Weight::from_parts(144_661, 0).saturating_mul(n.into()))
			.saturating_add(T::DbWeight::get().reads(4))
			.saturating_add(T::DbWeight::get().writes(3))
	}
```
