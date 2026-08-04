### Title
Relayer reward marked settled after unpaid fire-and-forget XCM send, with no confirmation that the beneficiary actually received funds - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
The external report's core defect is: a payout path relies on an external/untrusted execution step to move value to a beneficiary, but the caller treats the payout as final ("paid") without verifying the funds actually landed with the beneficiary. `PayAccountOnLocation::pay_reward` in `bridges/snowbridge/primitives/core/src/reward.rs` reproduces this pattern for Snowbridge relayer rewards: it only confirms that an XCM message was *handed to the router* (`XcmSender::deliver`), not that the `DepositAsset` instruction actually executed and credited the beneficiary on AssetHub.

### Finding Description
`PayAccountOnLocation::pay_reward` [1](#0-0)  builds an XCM program consisting of `UnpaidExecution`, `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, and `DepositAsset { beneficiary, .. }`, then calls `validate_send`, `XcmExecutor::charge_fees`, and `XcmSender::deliver`. The function returns `Ok(())` as soon as `deliver` succeeds — i.e., once the message is queued for the destination chain (AssetHub). It never confirms that:
- the destination chain actually accepted `UnpaidExecution` from this origin,
- `ReserveAssetDeposited` was honored (asset trusted/registered as reserve),
- `DepositAsset` succeeded and credited `beneficiary`.

This is implemented as a `PaymentProcedure<Relayer, (), RewardBalance>` used by the bridge-relayers pallet's `claim_rewards`/`claim_rewards_to` flow: `do_claim_rewards` removes the `RelayerRewards` entry via `try_mutate_exists`/`take()` and only re-inserts it (rolling back) if `PaymentProcedure::pay_reward` returns an `Err` [2](#0-1) . Because `PayAccountOnLocation::pay_reward` reports success on mere XCM delivery, any failure that occurs *after* delivery (destination rejects `UnpaidExecution`, asset not trusted as a reserve, insufficient weight, filtered/blocked beneficiary, `DepositAsset` failure, chain congestion causing the message to be dropped) is invisible to the relayers pallet. The `Event::RewardPaid` is emitted and the reward entry is deleted from `RelayerRewards`, permanently, even though the relayer received nothing.

Contrast this with the pattern the codebase itself uses elsewhere for exactly this class of problem — `pallet-treasury`'s `Paymaster` abstraction returns an opaque payment `Id`, keeps the spend in an `Attempted` state, and requires an explicit `check_status` call against `Paymaster::check_payment` before the spend is considered `Succeeded`/removed [3](#0-2) [4](#0-3) . `PayAccountOnLocation` does not implement this after-the-fact confirmation step at all — it conflates "message queued" with "value delivered," which is the same root cause as the reported LSSVM issue: the code assumes an external delivery mechanism transferred value correctly, without any balance/settlement check.

### Impact Explanation
If the XCM fails after being delivered to the transport layer (e.g. `UnpaidExecution` rejected on AssetHub for that origin, the reward asset isn't configured as an accepted reserve/teleport, `DepositAsset` weight/filter rejects the beneficiary, or the message is dropped/trapped on the remote side), the relayer's `RelayerRewards` entry is already deleted on the source chain by the time `pay_reward` returns `Ok(())`. The relayer loses the reward permanently with no retry path, since `claim_rewards`/`claim_rewards_to` require the storage entry to exist (`ok_or(Error::<T, I>::NoRewardForRelayer)`). This is a permanent, un-refundable loss of relayer reward funds — a value-conservation violation matching the "public underpriced work / bridge reward miscarriage" impact class (relayers doing real delivery/finality work receive no payout despite the protocol recording the reward as settled).

### Likelihood Explanation
No privileged actor, malicious relayer, or malicious validator is required. Any unprivileged relayer who successfully registers a reward and calls `claim_rewards`/`claim_rewards_to` can trigger this path. The failure condition (remote-side XCM execution failure after successful delivery) is a normal, expected outcome for XCM sends across chains — it is explicitly why `pallet-treasury` and `bridges/modules/relayers` other adapters use an attempt/confirm pattern (`PaymentStatus::InProgress`/`Failure`/`Success` + `check_status`), whereas `PayAccountOnLocation` skips that pattern entirely. No governance, admin, or malicious infrastructure action is needed — this is a systemic logic gap in a public dispatchable path.

### Recommendation
Do not treat `pay_reward` as complete on `XcmSender::deliver` success. Adopt the same async settlement pattern used by `pallet-treasury`/`pallet-bounties`:
- Return an opaque payment identifier (e.g., derived from the XCM message hash/query id) instead of `()`, and keep the relayer reward in an "Attempted" state instead of deleting `RelayerRewards` immediately.
- Use XCM `ReportError`/`QueryResponse` (or an equivalent settlement confirmation callback from AssetHub) to mark the reward as confirmed only after the destination reports successful execution/deposit.
- Add a `check_status`-equivalent extrinsic/hook that lets the reward be retried or restored if the destination reports failure, mirroring `Treasury::check_status` and `Treasury::payout`'s retry semantics.

### Proof of Concept
1. Configure a `pallet-bridge-relayers` instance with `PaymentProcedure = PayAccountOnLocation<...>` (as wired for Snowbridge V2 rewards, e.g. in `bridge_common_config.rs`).
2. Register a reward for `relayer` via `register_reward`/`process_delivery_receipt` (as in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` `process_delivery_receipt`) [5](#0-4) .
3. Call `claim_rewards_to` as the relayer, targeting a beneficiary/asset configuration on AssetHub that will make `DepositAsset` fail after delivery (e.g., a beneficiary account/location that the destination's asset filters reject, or an asset not registered as a trusted reserve on that route) while `XcmSender::deliver` itself still succeeds.
4. Observe: `pay_reward` returns `Ok(())` because only `deliver` was checked (as in `bridges/snowbridge/primitives/core/src/reward.rs:145-150`), `do_claim_rewards` commits the removal of the `RelayerRewards` entry, and `Event::RewardPaid`/`RewardRegistered` events fire — while the relayer's actual on-chain (AssetHub) balance never increases. Compare against the existing test `claim_snowbridge_rewards_to_local_account_fails` [6](#0-5) , which only covers the case where `pay_reward` itself returns an `Err` early (e.g. `FailedToPayReward`) — it does not cover, and the code does not guard against, the case where `deliver` succeeds but remote execution silently fails.

### Citations

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L127-151)
```rust
	fn pay_reward(
		relayer: &Relayer,
		_: (),
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		let ethereum_location = Location::new(2, [GlobalConsensus(EthereumNetwork::get())]);
		let assets: Asset = (ethereum_location.clone(), reward.into()).into();

		let xcm: Xcm<()> = alloc::vec![
			UnpaidExecution { weight_limit: Unlimited, check_origin: None },
			DescendOrigin(InboundQueueLocation::get().into()),
			UniversalOrigin(GlobalConsensus(EthereumNetwork::get())),
			ReserveAssetDeposited(assets.into()),
			DepositAsset { assets: AllCounted(1).into(), beneficiary },
		]
		.into();

		let (ticket, fee) =
			validate_send::<XcmSender>(AssetHubLocation::get(), xcm).map_err(|_| XcmSendFailure)?;
		XcmExecutor::charge_fees(relayer.clone(), fee).map_err(|_| ChargeFeesFailure)?;
		XcmSender::deliver(ticket).map_err(|_| XcmSendFailure)?;

		Ok(())
	}
```

**File:** bridges/modules/relayers/src/lib.rs (L263-300)
```rust
		fn do_claim_rewards(
			relayer: T::AccountId,
			reward_kind: T::Reward,
			beneficiary: BeneficiaryOf<T, I>,
		) -> DispatchResult {
			RelayerRewards::<T, I>::try_mutate_exists(
				&relayer,
				reward_kind,
				|maybe_reward| -> DispatchResult {
					let reward_balance =
						maybe_reward.take().ok_or(Error::<T, I>::NoRewardForRelayer)?;
					T::PaymentProcedure::pay_reward(
						&relayer,
						reward_kind,
						reward_balance,
						beneficiary.clone(),
					)
					.map_err(|e| {
						tracing::error!(
							target: LOG_TARGET,
							error=?e,
							?relayer,
							?reward_kind,
							?reward_balance,
							?beneficiary,
							"Failed to pay rewards"
						);
						Error::<T, I>::FailedToPayReward
					})?;

					Self::deposit_event(Event::<T, I>::RewardPaid {
						relayer: relayer.clone(),
						reward_kind,
						reward_balance,
						beneficiary,
					});
					Ok(())
				},
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-474)
```rust
			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}

```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L144-151)
```rust
		let reward_beneficiary = BridgeRewardBeneficiaries::LocalAccount(reward_address);
		let result = BridgeRelayers::claim_rewards_to(
			RuntimeOrigin::signed(relayer_account.clone()),
			BridgeReward::Snowbridge,
			reward_beneficiary.clone(),
		);
		assert_err!(result, FailedToPayReward::<Runtime, ()>);
	})
```
