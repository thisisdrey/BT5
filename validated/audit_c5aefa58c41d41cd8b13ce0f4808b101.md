Audit Report

## Title
Unbounded on-demand order queue rewrite on every scheduling advance causes underpriced, growing per-block work - (File: polkadot/runtime/parachains/src/on_demand/mod.rs)

## Summary
`OrderQueue::pop_assignment_for_cores` rewrites the entire remaining order queue on every call regardless of how many entries are actually popped, via `mem::take(&mut self.queue)`, a full iteration into `remaining_orders`, and `self.queue = BoundedVec::truncate_from(remaining_orders)` [1](#0-0) . This function is invoked every block from the scheduler's claim-queue advance path (`advance_assignments` → `AccessMode::pop_assignment_for_ondemand_cores` → `Pallet::pop_assignment_for_cores`) [2](#0-1) [3](#0-2) , so a permissionless attacker filling the queue near its bound inflates per-block scheduling cost.

## Finding Description
The queue is bounded by `config.scheduler_params.on_demand_queue_max_size`, and any signed account can push into it via `place_order_allow_death`, `place_order_keep_alive`, or `place_order_with_credits`, subject only to `ensure!(order_status.queue.len() < on_demand_queue_max_size, Error::<T>::QueueFull)` [4](#0-3) . Each call to `pop_assignment_for_cores` walks the *entire* current queue and rebuilds a new `Vec`/`BoundedVec` for whatever remains unpopped, even though `num_cores` (the actual number of items served that block) is typically much smaller than the queue length [1](#0-0) . The pallet's own documentation on the related `peek_order_queue` acknowledges this exact inefficiency: "the current implementation returns the entire queue (up to 10,000 orders)... Future implementations should consider adding a limit parameter to avoid returning unnecessary data" [5](#0-4) . The dispatchables' benchmarked weights (`WeightInfo::place_order_allow_death`, `place_order_keep_alive`, `place_order_with_credits`) only price the act of enqueuing an order [6](#0-5) ; none of them account for the recurring per-block O(queue length) cost imposed on every subsequent `advance_assignments` call while those orders remain queued.

## Impact Explanation
Because `advance_assignments` is called every block as part of the scheduler's claim-queue advance (itself part of the mandatory `paras_inherent` processing) [7](#0-6) , and `pop_assignment_for_cores` always re-serializes and rewrites the full `OrderStatus.queue` storage value [1](#0-0) , an attacker who fills the queue toward its configured maximum can force every subsequent block's scheduling step to pay work proportional to that queue depth instead of to the small number of cores actually served. This matches the "public underpriced work that degrades block production" impact category, since the cost is driven by attacker-controlled queue occupancy rather than by the bounded per-block work the scheduler is meant to perform.

## Likelihood Explanation
Placing orders requires only `ensure_signed` and sufficient balance/credits to cover the spot price up to the configured queue bound [8](#0-7) , with no validator, collator, or governance privilege needed. Repeatedly ordering for a para that is deliberately never granted a ready core (or simply keeping the queue populated) is a low-effort, repeatable strategy achievable by any funded account, sustaining the inflated per-block cost for as long as the attacker is willing to keep paying the spot price to refill the queue.

## Recommendation
Avoid rebuilding the entire queue vector on partial pops. Track a front cursor/index or use a deque-like structure so that only genuinely removed/reordered entries are touched, and bound per-call work by `num_cores * lookahead` rather than by total queue length; align the weight charged for scheduling advances (or for `place_order_*`) with the actual amortized cost of maintaining a large backlog.

## Proof of Concept
1. Attacker repeatedly calls `place_order_with_credits` for a `para_id` that never becomes ready (e.g., no core ever advances it), pushing entries via `OrderQueue::try_push` up to `on_demand_queue_max_size` [9](#0-8) .
2. Each block, the scheduler calls `pop_assignment_for_cores(now, num_cores)` with a small `num_cores` (e.g., 1 pool core), but the implementation still iterates and rewrites the full queue via `mem::take`/rebuild/`BoundedVec::truncate_from` [1](#0-0) .
3. Because the attacker's stuck orders are never served, they remain in `remaining_orders` and are rewritten to storage every block, imposing O(queue length) cost on the scheduling hot path for as long as the attacker sustains the backlog, at a cost not reflected in the fixed weight of the `place_order_*` calls that created the entries [6](#0-5) .

### Citations

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L106-131)
```rust
impl<N> OrderQueue<N> {
	/// Pop `num_cores` from the queue, assuming `now` as the current block number.
	pub fn pop_assignment_for_cores<T: Config>(
		&mut self,
		now: N,
		mut num_cores: u32,
	) -> impl Iterator<Item = ParaId>
	where
		N: Saturating + Ord + One + Copy,
	{
		let mut popped = BTreeSet::new();
		let mut remaining_orders = Vec::with_capacity(self.queue.len());
		for order in mem::take(&mut self.queue) {
			// Order is ready 2 blocks later (asynchronous backing):
			let ready_at = order.ordered_at.saturating_plus_one().saturating_plus_one();
			let is_ready = ready_at <= now;

			if num_cores > 0 && is_ready && popped.insert(order.para_id) {
				num_cores -= 1;
			} else {
				remaining_orders.push(order);
			}
		}
		self.queue = BoundedVec::truncate_from(remaining_orders);
		popped.into_iter()
	}
```

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L140-144)
```rust
	fn try_push(&mut self, now: N, para_id: ParaId) -> Result<(), ParaId> {
		self.queue
			.try_push(EnqueuedOrder { para_id, ordered_at: now })
			.map_err(|o| o.para_id)
	}
```

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L309-394)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(<T as Config>::WeightInfo::place_order_allow_death())]
		#[allow(deprecated)]
		#[deprecated(note = "This will be removed in favor of using `place_order_with_credits`")]
		pub fn place_order_allow_death(
			origin: OriginFor<T>,
			max_amount: BalanceOf<T>,
			para_id: ParaId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Pallet::<T>::do_place_order(
				sender,
				max_amount,
				para_id,
				AllowDeath,
				PaymentType::Balance,
			)
		}

		/// Same as the [`place_order_allow_death`](Self::place_order_allow_death) call , but with a
		/// check that placing the order will not reap the account.
		///
		/// Parameters:
		/// - `origin`: The sender of the call, funds will be withdrawn from this account.
		/// - `max_amount`: The maximum balance to withdraw from the origin to place an order.
		/// - `para_id`: A `ParaId` the origin wants to provide blockspace for.
		///
		/// Errors:
		/// - `InsufficientBalance`: from the Currency implementation
		/// - `QueueFull`
		/// - `SpotPriceHigherThanMaxAmount`
		///
		/// Events:
		/// - `OnDemandOrderPlaced`
		#[pallet::call_index(1)]
		#[pallet::weight(<T as Config>::WeightInfo::place_order_keep_alive())]
		#[allow(deprecated)]
		#[deprecated(note = "This will be removed in favor of using `place_order_with_credits`")]
		pub fn place_order_keep_alive(
			origin: OriginFor<T>,
			max_amount: BalanceOf<T>,
			para_id: ParaId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Pallet::<T>::do_place_order(
				sender,
				max_amount,
				para_id,
				KeepAlive,
				PaymentType::Balance,
			)
		}

		/// Create a single on demand core order with credits.
		/// Will charge the owner's on-demand credit account the spot price for the current block.
		///
		/// Parameters:
		/// - `origin`: The sender of the call, on-demand credits will be withdrawn from this
		///   account.
		/// - `max_amount`: The maximum number of credits to spend from the origin to place an
		///   order.
		/// - `para_id`: A `ParaId` the origin wants to provide blockspace for.
		///
		/// Errors:
		/// - `InsufficientCredits`
		/// - `QueueFull`
		/// - `SpotPriceHigherThanMaxAmount`
		///
		/// Events:
		/// - `OnDemandOrderPlaced`
		#[pallet::call_index(2)]
		#[pallet::weight(<T as Config>::WeightInfo::place_order_with_credits())]
		pub fn place_order_with_credits(
			origin: OriginFor<T>,
			max_amount: BalanceOf<T>,
			para_id: ParaId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Pallet::<T>::do_place_order(
				sender,
				max_amount,
				para_id,
				KeepAlive,
				PaymentType::Credits,
			)
		}
```

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L403-411)
```rust
	/// Pop assignments for the given number of on-demand cores in a block.
	pub fn pop_assignment_for_cores(
		now: BlockNumberFor<T>,
		num_cores: u32,
	) -> impl Iterator<Item = ParaId> {
		pallet::OrderStatus::<T>::mutate(|order_status| {
			order_status.queue.pop_assignment_for_cores::<T>(now, num_cores)
		})
	}
```

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L413-421)
```rust
	/// Look into upcoming orders.
	///
	/// The returned `OrderQueue` allows for simulating upcoming
	/// `pop_assignment_for_cores` calls.
	///
	/// **Note**: The current implementation returns the entire queue (up to 10,000 orders).
	/// Callers typically only need `num_cores * scheduling_lookahead` orders (e.g., 10 cores *
	/// 5 lookahead = 50 orders). Future implementations should consider adding a limit parameter
	/// to avoid returning unnecessary data and enable more efficient storage schemes.
```

**File:** polkadot/runtime/parachains/src/on_demand/mod.rs (L471-558)
```rust
	fn do_place_order(
		sender: <T as frame_system::Config>::AccountId,
		max_amount: BalanceOf<T>,
		para_id: ParaId,
		existence_requirement: ExistenceRequirement,
		payment_type: PaymentType,
	) -> DispatchResult {
		let config = configuration::ActiveConfig::<T>::get();

		pallet::OrderStatus::<T>::mutate(|order_status| {
			Self::update_spot_traffic(&config, order_status);
			let traffic = order_status.traffic;

			// Calculate spot price
			let spot_price: BalanceOf<T> = traffic.saturating_mul_int(
				config.scheduler_params.on_demand_base_fee.saturated_into::<BalanceOf<T>>(),
			);

			// Is the current price higher than `max_amount`
			ensure!(spot_price.le(&max_amount), Error::<T>::SpotPriceHigherThanMaxAmount);

			ensure!(
				order_status.queue.len() <
					config.scheduler_params.on_demand_queue_max_size as usize,
				Error::<T>::QueueFull
			);

			match payment_type {
				PaymentType::Balance => {
					// Charge the sending account the spot price. The amount will be teleported to
					// the broker chain once it requests revenue information.
					let amt = T::Currency::withdraw(
						&sender,
						spot_price,
						WithdrawReasons::FEE,
						existence_requirement,
					)?;

					// Consume the negative imbalance and deposit it into the pallet account. Make
					// sure the account preserves even without the existential deposit.
					let pot = Self::account_id();
					if !System::<T>::account_exists(&pot) {
						System::<T>::inc_providers(&pot);
					}
					T::Currency::resolve_creating(&pot, amt);
				},
				PaymentType::Credits => {
					let credits = Credits::<T>::get(&sender);

					// Charge the sending account the spot price in credits.
					let new_credits_value =
						credits.checked_sub(&spot_price).ok_or(Error::<T>::InsufficientCredits)?;

					if new_credits_value.is_zero() {
						Credits::<T>::remove(&sender);
					} else {
						Credits::<T>::insert(&sender, new_credits_value);
					}
				},
			}

			// Add the amount to the current block's (index 0) revenue information.
			Revenue::<T>::mutate(|bounded_revenue| {
				if let Some(current_block) = bounded_revenue.get_mut(0) {
					*current_block = current_block.saturating_add(spot_price);
				} else {
					// Revenue has already been claimed in the same block, including the block
					// itself. It shouldn't normally happen as revenue claims in the future are
					// not allowed.
					bounded_revenue.try_push(spot_price).defensive_ok();
				}
			});

			let now = <frame_system::Pallet<T>>::block_number();
			order_status
				.queue
				.try_push(now, para_id)
				.defensive_map_err(|_| Error::<T>::QueueFull)?;

			Pallet::<T>::deposit_event(Event::<T>::OnDemandOrderPlaced {
				para_id,
				spot_price,
				ordered_by: sender,
			});

			Ok(())
		})
	}
```

**File:** polkadot/runtime/parachains/src/scheduler/assigner_coretime/mod.rs (L279-294)
```rust
	/// Pop pool assignments according to access mode.
	fn pop_assignment_for_ondemand_cores(
		&mut self,
		now: BlockNumberFor<T>,
		num_cores: u32,
	) -> impl Iterator<Item = ParaId> {
		match self {
			Self::Peek { on_demand_orders } => on_demand_orders
				.pop_assignment_for_cores::<T>(now, num_cores)
				.collect::<Vec<_>>(),
			Self::Pop => {
				on_demand::Pallet::<T>::pop_assignment_for_cores(now, num_cores).collect::<Vec<_>>()
			},
		}
		.into_iter()
	}
```

**File:** polkadot/runtime/parachains/src/scheduler/assigner_coretime/mod.rs (L385-426)
```rust
pub(super) fn advance_assignments<T: Config, F: Fn(CoreIndex) -> bool>(
	is_blocked: F,
) -> BTreeMap<CoreIndex, ParaId> {
	let now = frame_system::Pallet::<T>::block_number();

	let assignments = super::CoreDescriptors::<T>::mutate(|core_states| {
		advance_assignments_single_impl::<T>(now, core_states, AccessMode::<T>::pop())
	});

	// Give blocked on-demand orders another chance:
	for blocked in assignments.pool_assignments.iter().filter_map(|(core_idx, para_id)| {
		if is_blocked(*core_idx) {
			Some(*para_id)
		} else {
			None
		}
	}) {
		on_demand::Pallet::<T>::push_back_order(blocked);
	}

	let mut assignments: BTreeMap<CoreIndex, ParaId> =
		assignments.into_iter().filter(|(core_idx, _)| !is_blocked(*core_idx)).collect();

	// Try to fill missing assignments from the next position (duplication to allow asynchronous
	// backing even for first assignment coming in on a previously empty core):
	let next = now.saturating_plus_one();
	let mut core_states = super::CoreDescriptors::<T>::get();
	let mut on_demand_orders = on_demand::Pallet::<T>::peek_order_queue();
	let next_assignments = advance_assignments_single_impl(
		next,
		&mut core_states,
		AccessMode::<T>::peek(&mut on_demand_orders),
	)
	.into_iter();

	for (core_idx, next_assignment) in
		next_assignments.filter(|(core_idx, _)| !is_blocked(*core_idx))
	{
		assignments.entry(core_idx).or_insert_with(|| next_assignment);
	}
	assignments
}
```
