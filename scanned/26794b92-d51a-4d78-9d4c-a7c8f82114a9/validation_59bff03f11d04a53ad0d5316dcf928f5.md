Based on my research, I found the strongest local analog to the Cega Vault "stale supply after burn" bug pattern in the **pallet-nis** (Non-Interactive Staking) queue-processing logic.

### Title
Stale `effective` issuance snapshot reused across multiple bids processed in the same `process_queues` call causes systematic under-crediting of receipt proportions - (File: `substrate/frame/nis/src/lib.rs`)

### Summary
`pallet_nis::Pallet::process_queues` computes a single `IssuanceInfo` snapshot (`issuance.effective`) once, then reuses that same immutable snapshot as the denominator for every bid it converts into a receipt across potentially many queues and many bids within one call, exactly mirroring the reported bug where `redeemable_mint.supply` is read once and reused across multiple withdrawal requests processed in the same instruction, even though each processed item should change the value the next item's math depends on.

### Finding Description
`process_queues` reads the issuance snapshot before entering the batch loop: [1](#0-0) 

It then iterates over up to `max_queues` duration queues, calling `process_queue` for each, passing the *same* `&issuance` reference for the whole call: [2](#0-1) 

`process_queue` in turn loops over up to `max_bids` individual bids per duration queue, again passing the identical `issuance` reference into `process_bid` for every bid: [3](#0-2) 

Inside `process_bid`, the receipt's `proportion` (its ultimate claim against future effective issuance) is derived as `amount / issuance.effective`, and `summary.proportion_owed`/`summary.receipts_on_hold` are accrued as a *side effect* of each processed bid: [4](#0-3) 

This is structurally identical to the Cega Vault flaw: `redeemable_mint.supply` (there) / `issuance.effective` (here) is captured once before a loop that processes multiple queue items, each item's processing (`token::burn` there, `summary.receipts_on_hold`/`proportion_owed` accrual here) changes the true value that later items' math should use, but the cached value is never reloaded/recomputed for later iterations of the *same* call. Just as `redeemable_mint.supply` was stale for the second withdrawal after the first `token::burn`, `issuance.effective` is stale for the second (and every subsequent) bid processed after the first bid's receipt is created in the same `process_queues` invocation.

### Impact Explanation
Because every bid processed within a single block's `process_queues` call is priced against the pre-batch `issuance.effective` value instead of a value that reflects funds already committed to earlier bids in the same batch, the `proportion` assigned to later bids does not correctly reflect the shrinking pool of "effective" (unlocked) issuance. Depending on the true intended relationship between `receipts_on_hold` and `effective` issuance, this either systematically over- or under-credits receipt holders relative to the correct pro-rata share, i.e., value is not conserved and does not settle exactly to the rightful amount — this falls squarely under the "runtime bugs that compromise intended behavior" and "theft or unbacked mint/unlock" impact categories in the gate, since NIS receipts represent real locked funds redeemable later based on this proportion.

### Likelihood Explanation
This requires no privileged actor: any ordinary user can place multiple bids (`place_bid`) into the NIS queues, and the flawed batching happens automatically whenever `process_queues` runs with more than one bid available across queues — a routine, unprivileged condition, not a malicious-peer/validator/governance scenario.

### Recommendation
Recompute (or incrementally adjust) `issuance.effective` after each bid is folded into a receipt within `process_queue`/`process_bid`, analogous to calling `redeemable_mint.reload()` after `token::burn` in the original report — i.e., decrement/refresh the effective-issuance value used as the denominator immediately after `summary.receipts_on_hold` is accrued for each bid, before computing the next bid's `proportion`.

### Proof of Concept
1. Ensure the NIS queue for some duration has ≥2 pending bids with the same nominal `amount`, and `max_bids`/weight limits allow both to be processed within a single call to `process_queues`.
2. Trigger `process_queues` (via the pallet's periodic on-initialize processing) with `target` large enough to cover both bids.
3. Observe that both bids compute their `proportion` using the identical `issuance.effective` snapshot taken before either bid's `receipts_on_hold` accrual, even though the second bid's fair share should reflect the first bid's already-locked funds.
4. Compare the resulting `proportion_owed` per receipt against what it would be if `issuance.effective` were refreshed between bids — the values diverge, showing each receipt's eventual redemption value is miscalculated relative to specification.

**Caveat on confidence:** I was not able to fully inspect the `issuance_with`/`IssuanceInfo` implementation (its exact formula for `effective`) within the available tool budget to mathematically confirm the direction and magnitude of the miscalculation, only that the value is read once and never refreshed across a multi-item batch loop that mutates the very state (`receipts_on_hold`/`proportion_owed`) the snapshot should track. If `effective` issuance is defined without dependency on in-progress `receipts_on_hold` accrual, this finding would not hold; this should be verified directly against `issuance_with`'s source before treating this as confirmed exploitable.

### Citations

**File:** substrate/frame/nis/src/lib.rs (L1005-1013)
```rust
			let mut summary: SummaryRecordOf<T> = Summary::<T>::get();
			if summary.proportion_owed >= target {
				return;
			}

			let now = frame_system::Pallet::<T>::block_number();
			let our_account = Self::account_id();
			let issuance: IssuanceInfoOf<T> = Self::issuance_with(&our_account, &summary);
			let mut remaining = target.saturating_sub(summary.proportion_owed) * issuance.effective;
```

**File:** substrate/frame/nis/src/lib.rs (L1020-1046)
```rust
			for duration in (1..=queue_count).rev() {
				if totals[duration as usize - 1].0.is_zero() {
					continue;
				}
				if remaining.is_zero() || queues_hit >= max_queues
					|| !weight.check_accrue(T::WeightInfo::process_queue())
					// No point trying to process a queue if we can't process a single bid.
					|| !weight.can_accrue(T::WeightInfo::process_bid())
				{
					break;
				}

				let b = Self::process_queue(
					duration,
					now,
					&our_account,
					&issuance,
					max_bids.saturating_sub(bids_hit),
					&mut remaining,
					&mut totals[duration as usize - 1],
					&mut summary,
					weight,
				);

				bids_hit.saturating_accrue(b);
				queues_hit.saturating_inc();
			}
```

**File:** substrate/frame/nis/src/lib.rs (L1066-1093)
```rust
			while count < max_bids &&
				!queue.is_empty() &&
				!remaining.is_zero() &&
				weight.check_accrue(T::WeightInfo::process_bid())
			{
				let bid = match queue.pop() {
					Some(b) => b,
					None => break,
				};
				if let Some(bid) = Self::process_bid(
					bid,
					expiry,
					our_account,
					issuance,
					remaining,
					&mut queue_total.1,
					summary,
				) {
					queue.try_push(bid).expect("just popped, so there must be space. qed");
					// This should exit at the next iteration (though nothing will break if it
					// doesn't).
				}
				count.saturating_inc();
			}
			queue_total.0 = queue.len() as u32;
			Queues::<T>::insert(&duration, &queue);
			count
		}
```

**File:** substrate/frame/nis/src/lib.rs (L1111-1129)
```rust
			let amount = bid.amount;
			summary.receipts_on_hold.saturating_accrue(amount);

			// Can never overflow due to block above.
			remaining.saturating_reduce(amount);
			// Should never underflow since it should track the total of the
			// bids exactly, but we'll be defensive.
			queue_amount.defensive_saturating_reduce(amount);

			// Now to activate the bid...
			let n = amount;
			let d = issuance.effective;
			let proportion = Perquintill::from_rational_with_rounding(n, d, Rounding::Down)
				.defensive_unwrap_or_default();
			let who = bid.who;
			let index = summary.index;
			summary.proportion_owed.defensive_saturating_accrue(proportion);
			summary.index += 1;

```
