[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L100-105)
```rust
//!
//! Slashing does not change any single member's balance. Instead, the slash will only reduce the
//! balance associated with a particular pool. But, we never change the total *points* of a pool
//! because of slashing. Therefore, when a slash happens, the ratio of points to balance changes in
//! a pool. In other words, the value of one point, which is initially 1-to-1 against a unit of
//! balance, is now less than one balance because of the slash.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3473-3522)
```rust
	/// Calculate the equivalent point of `new_funds` in a pool with `current_balance` and
	/// `current_points`.
	fn balance_to_point(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		new_funds: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		match (current_balance.is_zero(), current_points.is_zero()) {
			(_, true) => new_funds.saturating_mul(POINTS_TO_BALANCE_INIT_RATIO.into()),
			(true, false) => {
				// The pool was totally slashed.
				// This is the equivalent of `(current_points / 1) * new_funds`.
				new_funds.saturating_mul(current_points)
			},
			(false, false) => {
				// Equivalent to (current_points / current_balance) * new_funds
				balance(
					u256(current_points)
						.saturating_mul(u256(new_funds))
						// We check for zero above
						.div(u256(current_balance)),
				)
			},
		}
	}

	/// Calculate the equivalent balance of `points` in a pool with `current_balance` and
	/// `current_points`.
	fn point_to_balance(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		points: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		if current_balance.is_zero() || current_points.is_zero() || points.is_zero() {
			// There is nothing to unbond
			return Zero::zero();
		}

		// Equivalent of (current_balance / current_points) * points
		balance(
			u256(current_balance)
				.saturating_mul(u256(points))
				// We check for zero above
				.div(u256(current_points)),
		)
	}
```

**File:** substrate/frame/staking-async/src/ledger.rs (L428-482)
```rust
	pub(crate) fn slash(
		&mut self,
		slash_amount: BalanceOf<T>,
		minimum_balance: BalanceOf<T>,
		offence_era: EraIndex,
	) -> BalanceOf<T> {
		if slash_amount.is_zero() {
			return Zero::zero();
		}

		use sp_runtime::PerThing as _;
		let mut remaining_slash = slash_amount;
		let pre_slash_total = self.total;

		// for an `offence_era = x`, any chunk that is scheduled to be unlocked at era `x + 28`
		// (assuming 28 is the bonding duration) onwards should be slashed.
		let slashable_chunks_start = offence_era.saturating_add(T::BondingDuration::get());

		// `Some(ratio)` if this is proportional, with `ratio`, `None` otherwise. In both cases, we
		// slash first the active chunk, and then `slash_chunks_priority`.
		let (maybe_proportional, slash_chunks_priority) = {
			if let Some(first_slashable_index) =
				self.unlocking.iter().position(|c| c.era >= slashable_chunks_start)
			{
				// If there exists a chunk who's after the first_slashable_start, then this is a
				// proportional slash, because we want to slash active and these chunks
				// proportionally.

				// The indices of the first chunk after the slash up through the most recent chunk.
				// (The most recent chunk is at greatest from this era)
				let affected_indices = first_slashable_index..self.unlocking.len();
				let unbonding_affected_balance =
					affected_indices.clone().fold(BalanceOf::<T>::zero(), |sum, i| {
						if let Some(chunk) = self.unlocking.get(i).defensive() {
							sum.saturating_add(chunk.value)
						} else {
							sum
						}
					});
				let affected_balance = self.active.saturating_add(unbonding_affected_balance);
				let ratio = Perquintill::from_rational_with_rounding(
					slash_amount,
					affected_balance,
					Rounding::Up,
				)
				.unwrap_or_else(|_| Perquintill::one());
				(
					Some(ratio),
					affected_indices.chain((0..first_slashable_index).rev()).collect::<Vec<_>>(),
				)
			} else {
				// We just slash from the last chunk to the most recent one, if need be.
				(None, (0..self.unlocking.len()).rev().collect::<Vec<_>>())
			}
		};
```

**File:** substrate/frame/staking-async/src/ledger.rs (L495-519)
```rust
		let mut slash_out_of = |target: &mut BalanceOf<T>, slash_remaining: &mut BalanceOf<T>| {
			let mut slash_from_target = if let Some(ratio) = maybe_proportional {
				ratio.mul_ceil(*target)
			} else {
				*slash_remaining
			}
			// this is the total that that the slash target has. We can't slash more than
			// this anyhow!
			.min(*target)
			// this is the total amount that we would have wanted to slash
			// non-proportionally, a proportional slash should never exceed this either!
			.min(*slash_remaining);

			// slash out from *target exactly `slash_from_target`.
			*target = *target - slash_from_target;
			if *target < minimum_balance {
				// Slash the rest of the target if it's dust. This might cause the last chunk to be
				// slightly under-slashed, by at most `MaxUnlockingChunks * ED`, which is not a big
				// deal.
				slash_from_target =
					core::mem::replace(target, Zero::zero()).saturating_add(slash_from_target)
			}

			self.total = self.total.saturating_sub(slash_from_target);
			*slash_remaining = slash_remaining.saturating_sub(slash_from_target);
```
