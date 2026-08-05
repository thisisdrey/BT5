The code exactly matches the claim as described. The vulnerability is confirmed against the actual repository.Audit Report

## Title
Silent failure of the lottery payout transfer permanently strands the prize pot with no recovery path - (File: `substrate/frame/lottery/src/lib.rs`)

## Summary
`pallet-lottery`'s `on_initialize` hook pays the prize pot to the chosen winner via `T::Currency::transfer` but only checks the result with `debug_assert!`, which is compiled out in release builds. If the transfer fails (e.g., the winning account was reaped and the payout is below the Existential Deposit), the pallet still emits `Event::Winner`, kills `TicketsCount`, and resets/tears down the lottery config as though payout succeeded, leaving funds stranded in the pallet's derived account with no dispatchable to recover them.

## Finding Description
In `on_initialize` at [1](#0-0) , the payout transfer result is checked only via `debug_assert!(res.is_ok())`, which is a no-op in release builds. The pallet unconditionally proceeds to emit `Event::<T>::Winner`, kill `TicketsCount`, and either increment `LotteryIndex` and restart the round or set `*lottery = None`, regardless of whether the transfer actually succeeded.

`pot()` computes the claimable balance as `free_balance - minimum_balance` at [2](#0-1) , meaning any stranded balance left in the pallet account from a failed payout is folded into whatever pot exists for a subsequent lottery round, rather than returned to the intended winner.

The pallet's only four calls — `buy_ticket`, `set_calls`, `start_lottery`, and `stop_repeat`, defined at [3](#0-2)  — provide no mechanism to reclaim or retry a specific failed payout. This contrasts with the pattern already present elsewhere in the codebase, e.g. `pallet_bounties::reclaim_bounty_funds`, which permissionlessly sweeps residual balances from a bounty's dedicated account back to the treasury at [4](#0-3) .

An unprivileged participant can trigger this deterministically: buy the sole ticket in a round configured with a low `price` (a legitimate, non-adversarial manager parameter), then drain/reap their own account before the payout block so that `T::Currency::transfer` to the (now-nonexistent) winner for an amount below ED fails silently.

## Impact Explanation
The intended winner never receives the prize; funds are either permanently locked in the pallet's derived account (if the lottery is not restarted) or later paid to an unrelated future winner (if restarted), which is a wrong-beneficiary/duplicate-settlement outcome and a permanent-fund-lock scenario — both explicitly within the accepted impact gate. This is achievable purely through public extrinsics and standard balance transfers, with no privileged actor involved in the exploit trigger itself.

## Likelihood Explanation
Medium: requires a lottery configuration where `price * ticket_count` can fall below the Existential Deposit (plausible for low-value/promotional lotteries) and a participant willing to reap their own account before the payout block — both fully achievable by an ordinary user. Given these conditions, the bug fires deterministically because `debug_assert!` is inert in production builds.

## Recommendation
Replace the `debug_assert!(res.is_ok())` check with proper error handling: on transfer failure, avoid resetting `TicketsCount` and the lottery config as if the payout succeeded. Consider deferring the reset until payout is confirmed, retrying with a safer existence-preserving mode, or adding an explicit permissionless "reclaim stranded lottery funds" extrinsic analogous to `pallet_bounties::reclaim_bounty_funds`.

## Proof of Concept
1. `ManagerOrigin` calls `start_lottery(price = 1, length, delay, repeat = false)` with `price` below the runtime's Existential Deposit.
2. Account `A` calls `buy_ticket` once, becoming the sole ticket holder; the pot balance equals `price` (< ED).
3. Before the payout block, `A` transfers away its entire remaining balance, causing its account to be reaped.
4. At the payout block, `on_initialize` selects `A` as winner and calls `T::Currency::transfer(lottery_account, A, price, KeepAlive)`, which fails because `A` no longer exists and `price < ED`.
5. In a release build, `debug_assert!(res.is_ok())` is compiled out; the pallet still emits `Event::Winner`, kills `TicketsCount`, and sets `*lottery = None`.
6. The `price` amount remains permanently in the lottery pallet account (or is later folded into a future unrelated winner's payout if the manager restarts the lottery), with no extrinsic able to retrieve it for `A`.

### Citations

**File:** substrate/frame/lottery/src/lib.rs (L243-283)
```rust
		fn on_initialize(n: BlockNumberFor<T>) -> Weight {
			Lottery::<T>::mutate(|mut lottery| -> Weight {
				if let Some(config) = &mut lottery {
					let payout_block =
						config.start.saturating_add(config.length).saturating_add(config.delay);
					if payout_block <= n {
						let (lottery_account, lottery_balance) = Self::pot();

						let winner = Self::choose_account().unwrap_or(lottery_account);
						// Not much we can do if this fails...
						let res = T::Currency::transfer(
							&Self::account_id(),
							&winner,
							lottery_balance,
							KeepAlive,
						);
						debug_assert!(res.is_ok());

						Self::deposit_event(Event::<T>::Winner { winner, lottery_balance });

						TicketsCount::<T>::kill();

						if config.repeat {
							// If lottery should repeat, increment index by 1.
							LotteryIndex::<T>::mutate(|index| *index = index.saturating_add(1));
							// Set a new start with the current block.
							config.start = n;
							return T::WeightInfo::on_initialize_repeat();
						} else {
							// Else, kill the lottery storage.
							*lottery = None;
							return T::WeightInfo::on_initialize_end();
						}
						// We choose not need to kill Participants and Tickets to avoid a large
						// number of writes at one time. Instead, data persists between lotteries,
						// but is not used if it is not relevant.
					}
				}
				T::DbWeight::get().reads(1)
			})
		}
```

**File:** substrate/frame/lottery/src/lib.rs (L286-394)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T> {
		/// Buy a ticket to enter the lottery.
		///
		/// This extrinsic acts as a passthrough function for `call`. In all
		/// situations where `call` alone would succeed, this extrinsic should
		/// succeed.
		///
		/// If `call` is successful, then we will attempt to purchase a ticket,
		/// which may fail silently. To detect success of a ticket purchase, you
		/// should listen for the `TicketBought` event.
		///
		/// This extrinsic must be called by a signed origin.
		#[pallet::call_index(0)]
		#[pallet::weight(
			T::WeightInfo::buy_ticket()
				.saturating_add(call.get_dispatch_info().call_weight)
		)]
		pub fn buy_ticket(
			origin: OriginFor<T>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			let caller = ensure_signed(origin.clone())?;
			call.clone().dispatch(origin).map_err(|e| e.error)?;

			let _ = Self::do_buy_ticket(&caller, &call);
			Ok(())
		}

		/// Set calls in storage which can be used to purchase a lottery ticket.
		///
		/// This function only matters if you use the `ValidateCall` implementation
		/// provided by this pallet, which uses storage to determine the valid calls.
		///
		/// This extrinsic must be called by the Manager origin.
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::set_calls(calls.len() as u32))]
		pub fn set_calls(
			origin: OriginFor<T>,
			calls: Vec<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			T::ManagerOrigin::ensure_origin(origin)?;
			ensure!(calls.len() <= T::MaxCalls::get() as usize, Error::<T>::TooManyCalls);
			if calls.is_empty() {
				CallIndices::<T>::kill();
			} else {
				let indices = Self::calls_to_indices(&calls)?;
				CallIndices::<T>::put(indices);
			}
			Self::deposit_event(Event::<T>::CallsUpdated);
			Ok(())
		}

		/// Start a lottery using the provided configuration.
		///
		/// This extrinsic must be called by the `ManagerOrigin`.
		///
		/// Parameters:
		///
		/// * `price`: The cost of a single ticket.
		/// * `length`: How long the lottery should run for starting at the current block.
		/// * `delay`: How long after the lottery end we should wait before picking a winner.
		/// * `repeat`: If the lottery should repeat when completed.
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::start_lottery())]
		pub fn start_lottery(
			origin: OriginFor<T>,
			price: BalanceOf<T>,
			length: BlockNumberFor<T>,
			delay: BlockNumberFor<T>,
			repeat: bool,
		) -> DispatchResult {
			T::ManagerOrigin::ensure_origin(origin)?;
			Lottery::<T>::try_mutate(|lottery| -> DispatchResult {
				ensure!(lottery.is_none(), Error::<T>::InProgress);
				let index = LotteryIndex::<T>::get();
				let new_index = index.checked_add(1).ok_or(ArithmeticError::Overflow)?;
				let start = frame_system::Pallet::<T>::block_number();
				// Use new_index to more easily track everything with the current state.
				*lottery = Some(LotteryConfig { price, start, length, delay, repeat });
				LotteryIndex::<T>::put(new_index);
				Ok(())
			})?;
			// Make sure pot exists.
			let lottery_account = Self::account_id();
			if T::Currency::total_balance(&lottery_account).is_zero() {
				let _ =
					T::Currency::deposit_creating(&lottery_account, T::Currency::minimum_balance());
			}
			Self::deposit_event(Event::<T>::LotteryStarted);
			Ok(())
		}

		/// If a lottery is repeating, you can use this to stop the repeat.
		/// The lottery will continue to run to completion.
		///
		/// This extrinsic must be called by the `ManagerOrigin`.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::stop_repeat())]
		pub fn stop_repeat(origin: OriginFor<T>) -> DispatchResult {
			T::ManagerOrigin::ensure_origin(origin)?;
			Lottery::<T>::mutate(|mut lottery| {
				if let Some(config) = &mut lottery {
					config.repeat = false
				}
			});
			Ok(())
		}
	}
```

**File:** substrate/frame/lottery/src/lib.rs (L406-414)
```rust
	/// Return the pot account and amount of money in the pot.
	/// The existential deposit is not part of the pot so lottery account never gets deleted.
	fn pot() -> (T::AccountId, BalanceOf<T>) {
		let account_id = Self::account_id();
		let balance =
			T::Currency::free_balance(&account_id).saturating_sub(T::Currency::minimum_balance());

		(account_id, balance)
	}
```

**File:** substrate/frame/bounties/src/lib.rs (L1048-1090)
```rust
		///
		/// Permissionless. Moves all remaining assets from a closed bounty's account back to the
		/// treasury in a single call. Which assets are swept depends on the `TransferAllAssets`
		/// configuration.
		///
		/// The call is free if funds were reclaimed and paid otherwise, so no-op calls cannot be
		/// used to grief the network. Emits `BountyFundsReclaimed` on success.
		///
		/// ## Complexity
		/// - O(A) where A is the number of relevant assets configured in `TransferAllAssets`.
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
```
