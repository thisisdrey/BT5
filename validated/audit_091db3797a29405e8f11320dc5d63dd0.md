### Title
Snowbridge relayer reward claims are permanently cleared from storage before the XCM-based payout to the beneficiary is confirmed executed - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`pallet-bridge-relayers::claim_rewards`/`claim_rewards_to` permanently deletes the relayer's pending reward from `RelayerRewards` as soon as `PaymentProcedure::pay_reward` returns `Ok(())`. For the Snowbridge reward path, `pay_reward` is implemented by `PayAccountOnLocation`, which only enqueues an XCM message to AssetHub and returns `Ok(())` once the message has been **sent**, without any confirmation that the `DepositAsset` on AssetHub actually **executes**. This is the same broken invariant as the `disburseJob` bug: the internal accounting ("jobBalances"/"RelayerRewards") is decremented unconditionally while the actual value movement to the beneficiary is not guaranteed, leaving the relayer's reward permanently gone from bookkeeping with no guaranteed corresponding transfer.

### Finding Description
`do_claim_rewards` takes the reward value out of `RelayerRewards` inside a `try_mutate_exists` closure and calls the configured `T::PaymentProcedure::pay_reward`. Only if `pay_reward` returns an `Err` is the storage mutation rolled back: [1](#0-0) 

For the `BridgeReward::Snowbridge` variant, `pay_reward` is delegated to `snowbridge_core::reward::PayAccountOnLocation`: [2](#0-1) 

`PayAccountOnLocation::pay_reward` builds an XCM (`UnpaidExecution`, `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, `DepositAsset`) targeting AssetHub and returns `Ok(())` purely based on successful **queuing** of the message (`XcmSender::deliver`), never on its actual execution result: [3](#0-2) 

Unlike the `Pay`/`PayWithSource` abstraction used elsewhere in the codebase (e.g. treasury spends), which explicitly models asynchronous payment via `check_payment`/`PaymentStatus` and lets the caller retry on failure: [4](#0-3) [5](#0-4) 

the `bp_relayers::PaymentProcedure` trait used by `pallet-bridge-relayers` has **no such status-check/retry mechanism**: [6](#0-5) 

As a result, once the XCM is successfully enqueued (which requires only that the local `SendXcm` router and fee-charging succeed — conditions entirely within the relayer's/BridgeHub's control and unrelated to whether AssetHub's barrier/trust config, weight limits, reserve balances, or beneficiary account state permit the `DepositAsset` to succeed), the reward is irreversibly removed from `RelayerRewards`. If the AssetHub-side execution later fails for any reason (untrusted reserve asset, insufficient weight, beneficiary account below the existential deposit, sovereign/reserve pot insufficiently funded, barrier misconfiguration, etc.), the relayer's reward is silently and permanently lost with no local record and no way to retry, exactly mirroring the Escrow `disburseJob` inconsistency where `jobBalances` is depleted without a guaranteed corresponding transfer.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary and amount" invariant required for bridge reward payouts. A relayer who has legitimately earned a reward can permanently lose it due to a transient or persistent failure on the remote execution leg of the payout, with the local chain's bookkeeping showing the claim as fully settled (`RewardPaid` event emitted, storage entry removed) even though no funds were actually delivered. This is a permanent fund loss for bridge participants and, at protocol scale, undermines the relayer incentive mechanism that keeps Snowbridge message delivery running (an underpriced/failed-payout condition can degrade bridge processing incentives), which is within the accepted impact categories (permanent user-fund lock / bridge-state issue, duplicate/incorrect settlement).

### Likelihood Explanation
No malicious actor is required — the bug is triggerable by ordinary, unprivileged execution of `claim_rewards`/`claim_rewards_to` by any relayer with a legitimate reward. The failure mode only depends on downstream conditions (AssetHub-side barrier/trust/weight/balance state) that are outside of BridgeHub's control at the moment `pay_reward` is invoked, so any misconfiguration, temporary AssetHub congestion, or edge case (e.g., beneficiary account not meeting ED, asset not (yet) trusted) will reliably reproduce fund loss without requiring any privileged or malicious party.

### Recommendation
Do not remove/finalize the `RelayerRewards` entry (or otherwise treat the claim as settled) based solely on successful XCM message delivery. Adopt the same asynchronous `Pay`/`check_payment` pattern already used by `pallet-treasury` (`PaymentState::Attempted`/`check_status`) for the Snowbridge `PaymentProcedure`: track a payment id/attempt state, and only clear the reward once execution success is confirmed (e.g., via a receipt/callback from AssetHub, or a `Transact`+`ReportError` XCM pattern), providing a retry path if the remote execution fails.

### Proof of Concept
1. Configure a route where `BridgeRewardBeneficiaries::AssetHubLocation` conditions cause the AssetHub-side execution to fail after the message is delivered — e.g. beneficiary account does not meet AssetHub's existential deposit for the reserve asset, so `DepositAsset` traps on execution (this is a normal, attacker-reachable state — nothing requires the beneficiary account to pre-exist before claiming).
2. A relayer with `RelayerRewards` credit calls `claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(loc))`.
3. `do_claim_rewards` invokes `PayAccountOnLocation::pay_reward`, which validates and delivers the XCM ticket successfully and returns `Ok(())`, per [7](#0-6) .
4. `try_mutate_exists` commits, removing the reward entry and emitting `RewardPaid`, per [8](#0-7) .
5. On AssetHub, the XCM later fails to execute the `DepositAsset` (e.g., ED not met / barrier rejects), so the beneficiary never receives the reward asset.
6. Net result: `RelayerRewards` for the relayer is now empty (claim "successful") but no tokens were ever credited to the beneficiary — permanent loss, unrecoverable via any pallet call since there is no retry/check-status entry point for this `PaymentProcedure`.

### Citations

**File:** bridges/modules/relayers/src/lib.rs (L263-301)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-136)
```rust
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
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L126-151)
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

**File:** substrate/frame/support/src/traits/tokens/pay.rs (L44-65)
```rust
	/// Make a payment and return an identifier for later evaluation of success in some off-chain
	/// mechanism (likely an event, but possibly not on this chain).
	fn pay(
		who: &Self::Beneficiary,
		asset_kind: Self::AssetKind,
		amount: Self::Balance,
	) -> Result<Self::Id, Self::Error>;
	/// Check how a payment has proceeded. `id` must have been previously returned by `pay` for
	/// the result of this call to be meaningful.
	fn check_payment(id: Self::Id) -> PaymentStatus;
	/// Ensure that a call to pay with the given parameters will be successful if done immediately
	/// after this call. Used in benchmarking code.
	#[cfg(feature = "runtime-benchmarks")]
	fn ensure_successful(
		who: &Self::Beneficiary,
		asset_kind: Self::AssetKind,
		amount: Self::Balance,
	);
	/// Ensure that a call to `check_payment` with the given parameters will return either `Success`
	/// or `Failure`.
	#[cfg(feature = "runtime-benchmarks")]
	fn ensure_concluded(id: Self::Id);
```

**File:** substrate/frame/treasury/src/lib.rs (L794-813)
```rust

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

**File:** bridges/primitives/relayers/src/lib.rs (L114-130)
```rust
/// Reward payment procedure.
pub trait PaymentProcedure<Relayer, Reward, RewardBalance> {
	/// Error that may be returned by the procedure.
	type Error: Debug;

	/// Type parameter used to identify the beneficiaries eligible to receive rewards.
	type Beneficiary: Clone + Debug + Decode + Encode + Eq + TypeInfo;

	/// Pay reward to the relayer (or alternative beneficiary if provided) from the account with
	/// provided params.
	fn pay_reward(
		relayer: &Relayer,
		reward: Reward,
		reward_balance: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error>;
}
```
