## Analysis

The external report's core broken invariant: a system lets a user "schedule" a future claim on a shared pool of value (Moz) without reserving that value at schedule time, so unrelated later claimants can drain the pool before the original claimant executes their (earlier-approved) claim, making it unfulfillable.

The closest verified local analog is `pallet-treasury`'s `spend()` / `payout()` flow.

### Title
Treasury `spend()` approves payouts against a permission limit instead of the actual pot balance, letting unprivileged `payout()` callers drain the pot and starve earlier-approved spends - (File: substrate/frame/treasury/src/lib.rs)

### Summary
`Pallet::spend` records an `AssetSpendApproved` entry after checking only that the requested amount is within the caller's `SpendOrigin` permission (`max_amount`), never against the treasury's actual current balance (`Self::pot()`). Any signed account can later call the permissionless `payout()` for any approved spend index, which immediately calls `T::Paymaster::pay(...)` and transfers real funds out of the treasury account. Because approval does not reserve/earmark funds, multiple spends can be approved that collectively exceed the pot, and whichever `payout()` is submitted first consumes the available balance — exactly the "cut in line" pattern from the MozStaking report, applied to the treasury pot instead of a Moz balance.

### Finding Description
`spend()` [1](#0-0)  validates `native_amount <= max_amount` (the caller's `SpendOrigin` ceiling) and inserts a `SpendStatus{ Pending }` into `Spends`, but it never checks or reduces `Self::pot()`, and never places a hold on the treasury account. Contrast this with the deprecated `spend_local`/`spend_funds` path, which processes the `Approvals` queue strictly in insertion order and only pays proposals while `budget_remaining` allows it [2](#0-1)  — that path preserves first-approved-first-paid ordering. The newer `spend`/`payout` flow has no such ordering guarantee.

`payout()` is dispatchable by any signed account (not just the beneficiary), for any spend index, at any time within its validity window [3](#0-2) . It directly invokes `T::Paymaster::pay(&spend.beneficiary, ...)`, which pulls from the treasury account's real balance. Since no funds were reserved when the spend was *approved*, the actual availability of funds is only checked at claim time — first-claimed, first-served, regardless of approval order.

`Self::pot()` is simply the treasury account's free balance minus ED [4](#0-3) , with no per-spend accounting/reservation layer analogous to the "reserved Moz amount" the report recommends.

The project's own `pr_7959.prdoc` documents awareness of the resulting problem [5](#0-4) : "if someone attempts to claim a valid payout and there isn't sufficient liquidity to fulfill it," the fix only resets the expiry window on failed attempts (`check_status`/`payout` retry loop) [6](#0-5)  — it does not reserve funds at approval time, so the underlying "spend approved without earmarked funds" invariant break remains.

### Impact Explanation
When multiple spends are approved close together (a common governance/OpenGov pattern, e.g. many small `spend()` treasury track approvals), an attacker or simply a faster/automated claimant can call `payout()` on a later-approved-but-cheaper-to-claim spend first, draining the pot. An earlier-approved, legitimate beneficiary's `payout()` then fails with `PayoutError`, forcing them into repeated `check_status`/retry cycles while liquidity is unpredictable, and their claim can remain unfulfillable for extended periods if the pot is not sufficiently replenished — a denial of expected payout / fund-availability race, matching the report's "cut in line" and "unfulfillable redemption" class, on live-scope Treasury value.

### Likelihood Explanation
No privileged actor, governance abuse, or malicious node/validator is required. `payout()` is `ensure_signed`-only and permissionless for any approved spend index; the race condition arises naturally whenever pot liquidity is tight relative to outstanding approvals (a state governance/OpenGov spend velocity can easily create), and can also be deliberately triggered by any user submitting `payout()` calls for their own approved (but chronologically later) spend ahead of an earlier approved spend belonging to someone else.

### Recommendation
Reserve/earmark the approved amount from the pot's accounting at `spend()` time (e.g., track a `TotalPendingSpend` running total subtracted from `pot()` and reject new `spend()` calls once cumulative pending spends would exceed available balance), mirroring the reservation fix recommended for MozStaking. Alternatively, restore FIFO settlement guarantees for the `spend`/`payout` flow so earlier approvals are guaranteed priority over later ones when liquidity is insufficient.

### Proof of Concept
1. Treasury pot has balance `X`.
2. Governance approves `spend(amount=X, beneficiary=Alice)` at block `N` → `Spends[0]` pending, no funds moved/reserved.
3. Governance approves `spend(amount=X, beneficiary=Bob)` at block `N+1` → `Spends[1]` pending (also accepted, since only checked against `SpendOrigin::max_amount`, not against `pot()`).
4. Bob (or anyone) calls `payout(1)` first → `T::Paymaster::pay` succeeds, drains the pot to (near) zero.
5. Alice calls `payout(0)` → `T::Paymaster::pay` fails due to insufficient treasury balance → `Error::PayoutError`; Alice must repeatedly call `check_status`/`payout` until the pot is replenished, exactly reproducing the "cut in line" / unfulfillable redemption pattern described in the external report.

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L657-713)
```rust
		) -> DispatchResult {
			let max_amount = T::SpendOrigin::ensure_origin(origin)?;
			let beneficiary = T::BeneficiaryLookup::lookup(*beneficiary)?;

			let now = T::BlockNumberProvider::current_block_number();
			let valid_from = valid_from.unwrap_or(now);
			let expire_at = valid_from.saturating_add(T::PayoutPeriod::get());
			ensure!(expire_at > now, Error::<T, I>::SpendExpired);

			let native_amount =
				T::BalanceConverter::from_asset_balance(amount, *asset_kind.clone())
					.map_err(|_| Error::<T, I>::FailedToConvertBalance)?;

			ensure!(native_amount <= max_amount, Error::<T, I>::InsufficientPermission);

			with_context::<SpendContext<BalanceOf<T, I>>, _>(|v| {
				let context = v.or_default();
				// We group based on `max_amount`, to distinguish between different kind of
				// origins. (assumes that all origins have different `max_amount`)
				//
				// Worst case is that we reject some "valid" request.
				let spend = context.spend_in_context.entry(max_amount).or_default();

				// Ensure that we don't overflow nor use more than `max_amount`
				if spend.checked_add(&native_amount).map(|s| s > max_amount).unwrap_or(true) {
					Err(Error::<T, I>::InsufficientPermission)
				} else {
					*spend = spend.saturating_add(native_amount);
					Ok(())
				}
			})
			.unwrap_or(Ok(()))?;

			let index = SpendCount::<T, I>::get();
			Spends::<T, I>::insert(
				index,
				SpendStatus {
					asset_kind: *asset_kind.clone(),
					amount,
					beneficiary: beneficiary.clone(),
					valid_from,
					expire_at,
					status: PaymentState::Pending,
				},
			);
			SpendCount::<T, I>::put(index + 1);

			Self::deposit_event(Event::AssetSpendApproved {
				index,
				asset_kind: *asset_kind,
				amount,
				beneficiary,
				valid_from,
				expire_at,
			});
			Ok(())
		}
```

**File:** substrate/frame/treasury/src/lib.rs (L736-757)
```rust
		pub fn payout(origin: OriginFor<T>, index: SpendIndex) -> DispatchResult {
			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);

			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
			Spends::<T, I>::insert(index, spend);

			Self::deposit_event(Event::<T, I>::Paid { index, payment_id: id });

			Ok(())
		}
```

**File:** substrate/frame/treasury/src/lib.rs (L778-814)
```rust
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::check_status())]
		pub fn check_status(origin: OriginFor<T>, index: SpendIndex) -> DispatchResultWithPostInfo {
			use PaymentState as State;
			use PaymentStatus as Status;

			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();

			if now > spend.expire_at && !matches!(spend.status, State::Attempted { .. }) {
				// spend has expired and no further status update is expected.
				Spends::<T, I>::remove(index);
				Self::deposit_event(Event::<T, I>::SpendProcessed { index });
				return Ok(Pays::No.into());
			}

			let payment_id = match spend.status {
				State::Attempted { id } => id,
				_ => return Err(Error::<T, I>::NotAttempted.into()),
			};

			match T::Paymaster::check_payment(payment_id) {
				Status::Failure => {
					spend.status = PaymentState::Failed;
					Spends::<T, I>::insert(index, spend);
					Self::deposit_event(Event::<T, I>::PaymentFailed { index, payment_id });
				},
				Status::Success | Status::Unknown => {
					Spends::<T, I>::remove(index);
					Self::deposit_event(Event::<T, I>::SpendProcessed { index });
					return Ok(Pays::No.into());
				},
				Status::InProgress => return Err(Error::<T, I>::Inconclusive.into()),
			}
			return Ok(Pays::Yes.into());
		}
```

**File:** substrate/frame/treasury/src/lib.rs (L921-952)
```rust
		let proposals_len = Approvals::<T, I>::mutate(|v| {
			let proposals_approvals_len = v.len() as u32;
			v.retain(|&index| {
				// Should always be true, but shouldn't panic if false or we're screwed.
				if let Some(p) = Proposals::<T, I>::get(index) {
					if p.value <= budget_remaining {
						budget_remaining -= p.value;
						Proposals::<T, I>::remove(index);

						// return their deposit.
						let err_amount = T::Currency::unreserve(&p.proposer, p.bond);
						debug_assert!(err_amount.is_zero());

						// provide the allocation.
						imbalance.subsume(T::Currency::deposit_creating(&p.beneficiary, p.value));

						Self::deposit_event(Event::Awarded {
							proposal_index: index,
							award: p.value,
							account: p.beneficiary,
						});
						false
					} else {
						missed_any = true;
						true
					}
				} else {
					false
				}
			});
			proposals_approvals_len
		});
```

**File:** substrate/frame/treasury/src/lib.rs (L997-1003)
```rust
	/// Return the amount of money in the pot.
	// The existential deposit is not part of the pot so treasury account never gets deleted.
	pub fn pot() -> BalanceOf<T, I> {
		T::Currency::free_balance(&Self::account_id())
			// Must never be less than 0 but better be safe.
			.saturating_sub(T::Currency::minimum_balance())
	}
```

**File:** prdoc/stable2503/pr_7959.prdoc (L1-10)
```text
title: Update expire date on treasury payout
doc:
- audience: Runtime Dev
  description: |-
    Resets the `payout.expire_at` field with the `PayoutPeriod` every time that there is a valid Payout attempt.
    Prior to this change, when a spend is approved, it receives an expiry date so that if it’s never claimed, it automatically expires. This makes sense under normal circumstances. However, if someone attempts to claim a valid payout and there isn’t sufficient liquidity to fulfill it, the expiry date currently remains unchanged. This effectively penalizes the claimant in the same way as if they had never requested the payout in the first place.
    With this change users are not penalized for liquidity shortages and have a fair window to claim once the funds are available.
crates:
- name: pallet-treasury
  bump: patch
```
