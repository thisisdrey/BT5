This confirms the technical mechanics of the claim. `PayAccountOnLocation::pay_reward` in `bridges/snowbridge/primitives/core/src/reward.rs` returns `Ok(())` immediately after `XcmSender::deliver(ticket)` succeeds, without any confirmation of remote execution.Audit Report

## Title
Reward ledger entry is deleted on successful XCM *send*, not on confirmed settlement — relayer reward can be irrecoverably lost/trapped when the cross-chain deposit fails - (File: `bridges/modules/relayers/src/lib.rs`)

## Summary
`pallet-bridge-relayers`' `do_claim_rewards` permanently removes a relayer's reward entry from `RelayerRewards` inside the same `try_mutate_exists` closure that calls `T::PaymentProcedure::pay_reward`, treating any `Ok(())` return as final settlement. For the Snowbridge reward path, `PayAccountOnLocation::pay_reward` returns `Ok(())` as soon as the XCM carrying `ReserveAssetDeposited` + `DepositAsset { beneficiary }` is successfully handed to `XcmSender::deliver`, which only guarantees the message was queued for delivery to AssetHub — not that `DepositAsset` executed successfully there.

## Finding Description
`do_claim_rewards` takes the reward out of storage via `maybe_reward.take()` and only restores it if `pay_reward` itself returns an `Err`: [1](#0-0) 

For the Snowbridge `Reward` kind, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which builds an XCM ending in `DepositAsset { assets: AllCounted(1).into(), beneficiary }` and returns `Ok(())` immediately once `XcmSender::deliver(ticket)` succeeds — with no confirmation that the remote `DepositAsset` execution on AssetHub actually credited the beneficiary: [2](#0-1) 

The XCM program itself has no `SetErrorHandler`/`SetAppendix` fallback and uses `UnpaidExecution` with `DescendOrigin`/`UniversalOrigin(GlobalConsensus(Ethereum))` as the executing origin on AssetHub: [3](#0-2) 

If `DepositAsset` fails on AssetHub (e.g., beneficiary lacks existential deposit for the bridged asset, or the destination `Location` cannot hold assets), the assets in holding are trapped by the executor's `DropAssets`/`ClaimAssets` mechanism, keyed by the executing origin at the point of failure: [4](#0-3) 

That origin is the descended/`UniversalOrigin(Ethereum)` context, not an origin the relayer or a normal account can reconstruct and present via `ClaimAssets`. Meanwhile, back on BridgeHub, the ledger entry was already deleted and the `RewardPaid` event already emitted before any of this remote execution occurs — there is no intermediate "attempted" state and no retry path, unlike `pallet-treasury`'s `payout`/`check_status` flow, which explicitly models asynchronous settlement via `PaymentState::Attempted { id }` and only clears the spend after `Paymaster::check_payment` confirms success: [5](#0-4) [6](#0-5) 

`bp_relayers::PaymentProcedure` (as implemented by `BridgeRewardPayer` in the bridge-hub-westend runtime, dispatching to `PayAccountOnLocation` for the `Snowbridge` reward kind and `AssetHubLocation` beneficiary) has no equivalent in-flight/check-status concept; a single `Result<(), Error>` is treated as final settlement: [7](#0-6) 

The existing emulated test suite has to deliberately fund reward amounts above `ETHER_MIN_BALANCE` to avoid tripping this exact failure mode, confirming the fragility is reachable under ordinary, non-adversarial conditions: [8](#0-7) 

## Impact Explanation
This is a genuine violation of the "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant. The corrupted/incorrectly-advanced value is the `RelayerRewards` storage entry (double map keyed by relayer account and `Reward` kind) — it is cleared and the reward permanently forfeited even though the actual value transfer to the beneficiary on AssetHub can silently fail and become trapped under an origin the relayer cannot claim from. This matches the "permanent user-fund or bridge-state lock" impact category: a normal, non-privileged relayer calling a public extrinsic (`claim_rewards`/`claim_rewards_to`) can permanently lose an earned reward with no compensating recovery mechanism in `pallet-bridge-relayers` or the Snowbridge `PayAccountOnLocation` procedure.

## Likelihood Explanation
No malicious cooperation or privileged access is required. Any relayer specifying (or the runtime resolving to) an `AssetHubLocation` beneficiary account lacking the destination foreign asset's existential deposit — a very ordinary condition for a fresh or rarely-used account — triggers the failure path deterministically. The project's own integration tests had to work around this exact scenario by pre-funding beneficiaries above `ETHER_MIN_BALANCE`, demonstrating the condition is easily reachable in normal, non-adversarial operation, making this a highly likely occurrence rather than a theoretical edge case.

## Recommendation
Do not remove the `RelayerRewards` entry (or otherwise treat the reward as finally settled) based solely on `pay_reward`'s `Ok(())` when the underlying procedure is asynchronous/XCM-routed, as is the case for `PayAccountOnLocation`. Introduce an intermediate "attempted" state analogous to `pallet-treasury`'s `PaymentState::Attempted { id }`, and only clear the relayer's claim once remote execution/settlement is confirmed (e.g., via a delivery/execution receipt or a `check_status`-style callback), with a defined fallback to a retryable/failed state — and ideally a reachable claim path — if the remote `DepositAsset` traps.

## Proof of Concept
1. A relayer accrues a Snowbridge reward via `register_reward` in `bridges/modules/relayers/src/lib.rs`.
2. The relayer calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(some_location))` where `some_location` maps to an AssetHub account without the existential deposit for the ETH-derived foreign asset.
3. `do_claim_rewards` invokes `BridgeRewardPayer::pay_reward` → `PayAccountOnLocation::pay_reward`, which successfully builds and delivers the XCM (`Ok(())`), unconditionally clearing `RelayerRewards` and emitting `RewardPaid`.
4. On AssetHub, `DepositAsset` fails to credit the beneficiary; the assets are trapped under the `UniversalOrigin(GlobalConsensus(Ethereum))` execution context, unreachable by the relayer via `ClaimAssets`.
5. The relayer has no reward left in storage (subsequent claims return `NoRewardForRelayer`) and no path to recover the trapped value, demonstrating permanent, unrecoverable fund loss despite the runtime's ledger recording the reward as paid.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-302)
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
			)
		}
```

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

**File:** polkadot/xcm/xcm-executor/src/traits/drop_assets.rs (L27-82)
```rust
pub trait DropAssets {
	/// Handler for receiving dropped assets. Returns the weight consumed by this operation.
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight;
}
impl DropAssets for () {
	fn drop_assets(_origin: &Location, _assets: AssetsInHolding, _context: &XcmContext) -> Weight {
		Weight::zero()
	}
}

/// Morph a given `DropAssets` implementation into one which can filter based on assets. This can
/// be used to ensure that `AssetsInHolding` values which hold no value are ignored.
#[allow(dead_code)]
pub struct FilterAssets<D, A>(PhantomData<(D, A)>);

impl<D: DropAssets, A: Contains<AssetsInHolding>> DropAssets for FilterAssets<D, A> {
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight {
		if A::contains(&assets) {
			D::drop_assets(origin, assets, context)
		} else {
			Weight::zero()
		}
	}
}

/// Morph a given `DropAssets` implementation into one which can filter based on origin. This can
/// be used to ban origins which don't have proper protections/policies against misuse of the
/// asset trap facility don't get to use it.
#[allow(dead_code)]
pub struct FilterOrigin<D, O>(PhantomData<(D, O)>);

impl<D: DropAssets, O: Contains<Location>> DropAssets for FilterOrigin<D, O> {
	fn drop_assets(origin: &Location, assets: AssetsInHolding, context: &XcmContext) -> Weight {
		if O::contains(origin) {
			D::drop_assets(origin, assets, context)
		} else {
			Weight::zero()
		}
	}
}

/// Define any handlers for the `AssetClaim` instruction.
///
/// Types implementing this trait should make sure to properly handle imbalances held within the
/// trap and pass them over to `AssetsInHolding`. Generally should have a mirror `DropAssets`
/// implementation that originally moved the imbalance from holding to this trap.
pub trait ClaimAssets {
	/// Claim any assets available to `origin` and return them in a single `AssetsInHolding` value,
	/// together with the weight used by this operation.
	fn claim_assets(
		origin: &Location,
		ticket: &Location,
		what: &Assets,
		context: &XcmContext,
	) -> Option<AssetsInHolding>;
}
```

**File:** substrate/frame/treasury/src/lib.rs (L734-757)
```rust
		#[pallet::call_index(6)]
		#[pallet::weight(T::WeightInfo::payout())]
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

**File:** substrate/frame/treasury/src/lib.rs (L778-813)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L89-140)
```rust
/// Implementation of `bp_relayers::PaymentProcedure` as a pay/claim rewards scheme.
pub struct BridgeRewardPayer;
impl bp_relayers::PaymentProcedure<AccountId, BridgeReward, u128> for BridgeRewardPayer {
	type Error = sp_runtime::DispatchError;
	type Beneficiary = BridgeRewardBeneficiaries;

	fn pay_reward(
		relayer: &AccountId,
		reward_kind: BridgeReward,
		reward: u128,
		beneficiary: BridgeRewardBeneficiaries,
	) -> Result<(), Self::Error> {
		match reward_kind {
			BridgeReward::RococoWestend(lane_params) => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(account) => {
						bp_relayers::PayRewardFromAccount::<
							Balances,
							AccountId,
							LegacyLaneId,
							u128,
						>::pay_reward(
							&relayer, lane_params, reward, account,
						)
					},
					BridgeRewardBeneficiaries::AssetHubLocation(_) => Err(Self::Error::Other("`AssetHubLocation` beneficiary is not supported for `RococoWestend` rewards!")),
				}
			},
			BridgeReward::Snowbridge => {
				match beneficiary {
					BridgeRewardBeneficiaries::LocalAccount(_) => Err(Self::Error::Other("`LocalAccount` beneficiary is not supported for `Snowbridge` rewards!")),
					BridgeRewardBeneficiaries::AssetHubLocation(account_location) => {
						let account_location = Location::try_from(account_location)
							.map_err(|_| Self::Error::Other("`AssetHubLocation` beneficiary location version is not supported for `Snowbridge` rewards!"))?;
						snowbridge_core::reward::PayAccountOnLocation::<
							AccountId,
							u128,
							EthereumNetwork,
							AssetHubLocation,
							InboundQueueV2Location,
							XcmRouter,
							XcmExecutor<XcmConfig>,
							RuntimeCall
						>::pay_reward(
							relayer, (), reward, account_location
						)
					}
				}
			}
		}
	}
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs (L39-49)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;
		type RuntimeOrigin = <BridgeHubWestend as Chain>::RuntimeOrigin;
		let reward_amount = ETHER_MIN_BALANCE * 2; // Reward should be more than Ether min balance

		type BridgeRelayers = <BridgeHubWestend as BridgeHubWestendPallet>::BridgeRelayers;
		BridgeRelayers::register_reward(
			(&relayer_account.clone()).into(),
			BridgeReward::Snowbridge,
			reward_amount,
		);
```
