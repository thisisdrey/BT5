### Title
Unvalidated XCM `beneficiary` Location in Snowbridge reward claim causes irreversible reward burn on delivery failure - ([File: bridges/modules/relayers/src/lib.rs])

### Summary
`pallet-bridge-relayers::claim_rewards_to` removes the relayer's accumulated reward from storage and only rolls back that removal if `PaymentProcedure::pay_reward` returns an `Err`. For the Snowbridge `AssetHubLocation` beneficiary path, `pay_reward` (`PayAccountOnLocation::pay_reward`) reports success as soon as the XCM is *sent* to AssetHub (`XcmSender::deliver`), without any confirmation that the remote `DepositAsset` to the caller-supplied `beneficiary: Location` actually succeeds. This mirrors the external report's root cause: a value (there, `fee_collector_vault`; here, the reward `beneficiary` `Location`) is accepted and used to gate/settle a payout without validating that it is actually resolvable/depositable, so the payout can silently fail while the bookkeeping already marks it as settled/consumed.

### Finding Description
`do_claim_rewards` in [1](#0-0)  takes the reward out of `RelayerRewards` storage inside a `try_mutate_exists` closure and calls `T::PaymentProcedure::pay_reward(...)`. Only if that call returns `Err` is the mutation rolled back; on `Ok(())` the reward entry stays removed and `Event::RewardPaid` is emitted.

For `BridgeReward::Snowbridge`, the beneficiary type is `BridgeRewardBeneficiaries::AssetHubLocation(VersionedLocation)`, and `pay_reward` is implemented in `BridgeRewardPayer::pay_reward` in [2](#0-1) . The only check performed on the caller-supplied location is a version conversion (`Location::try_from(account_location)`) — there is no check that the resulting `Location` is a well-formed, depositable account on AssetHub.

That location is passed straight into `PayAccountOnLocation::pay_reward` in [3](#0-2) , which builds a fire-and-forget XCM ending in `DepositAsset { assets: AllCounted(1).into(), beneficiary }` and calls `validate_send` + `XcmSender::deliver`. The function returns `Ok(())` as soon as the message is *delivered into the transport*, mapping only `XcmSendFailure`/`ChargeFeesFailure` (defined in [4](#0-3) ) to an `Err`. Whether the remote `DepositAsset` instruction actually succeeds on AssetHub (e.g., because the `beneficiary` `Location` doesn't resolve to a depositable account, has unsupported junction structure, or the asset gets trapped) is never observed by BridgeHub.

Because `do_claim_rewards` already deleted the `RelayerRewards` entry and emitted `RewardPaid` based on the `Ok(())` from `pay_reward`, the reward accounting has "settled" before the actual on-chain execution/settlement of the mint is confirmed — violating the invariant that payout state should only advance after execution and settlement succeed atomically. If the remote deposit fails, the value is stranded/trapped on AssetHub with no path back to the relayer, and the relayer's `RelayerRewards` entry is already gone, so nothing can be retried.

### Impact Explanation
This is a public, unprivileged, self-serviceable entry point (`claim_rewards_to`, `ensure_signed`) reachable by any relayer with a pending reward. A malformed or unresolvable `beneficiary` Location — whether from relayer tooling bugs, incompatible location encodings, or unexpected junction shapes — leads to permanent loss of the relayer's earned reward: the local ledger is decremented/removed and reports success via `RewardPaid`, while the corresponding remote mint may never land in an owned account. This is a fund-loss / no-settlement-guarantee bug consistent with the "duplicate settlement" and "payout state must only advance after settlement succeeds atomically" pivots called out in the assessment guidance.

### Likelihood Explanation
The path only requires a relayer to call the already-public `claim_rewards_to` with a beneficiary Location that is syntactically valid (passes `VersionedLocation` conversion) but not structurally depositable on the remote chain — no admin, governance, relayer-node compromise, or malicious peer is required. Given the number of valid `Location` encodings that are not actually resolvable to a receiving account on AssetHub (arbitrary junction combinations, non-account interior types, or locations that reference chains/paths not supported for deposit), accidental or edge-case triggering is plausible, especially as relayer tooling evolves or when integrating additional location kinds.

### Recommendation
- Do not remove/finalize the `RelayerRewards` entry (nor emit `RewardPaid`) purely on `XcmSender::deliver` success; require confirmation of actual settlement (e.g., via a receipt/callback pattern similar to `pallet-xcm`'s query mechanism, or restrict `AssetHubLocation` beneficiaries to a validated whitelist of resolvable account shapes).
- Add explicit structural validation of the `beneficiary: Location` before invoking `pay_reward` — ensure it resolves to a concrete `AccountId32`/`AccountKey20` junction reachable via `DepositAsset`, rejecting arbitrary/complex locations.
- Consider making the payout process retryable/recoverable (mirroring `pallet-treasury`'s `PaymentState::Failed` + `check_status` pattern already used elsewhere in this repo) instead of one-shot fire-and-forget settlement.

### Proof of Concept
1. A relayer accumulates a `BridgeReward::Snowbridge` reward via normal message delivery (see `RewardRegistered` flow in `EthereumOutboundQueueV2::process_delivery_receipt`).
2. The relayer calls `pallet_bridge_relayers::claim_rewards_to(origin, BridgeReward::Snowbridge, BridgeRewardBeneficiaries::AssetHubLocation(some_malformed_but_version-valid_location))`.
3. `BridgeRewardPayer::pay_reward` converts the `VersionedLocation` successfully (no deeper validation) and calls `PayAccountOnLocation::pay_reward`, which builds and sends the XCM; `XcmSender::deliver` succeeds, so `pay_reward` returns `Ok(())`.
4. `do_claim_rewards` removes the `RelayerRewards` entry and emits `RewardPaid`.
5. On AssetHub, the `DepositAsset { beneficiary }` instruction in the remote XCM fails to resolve/deposit to the malformed location (e.g., traps the asset or errors), and the relayer never receives their reward — with no remaining state on BridgeHub to detect or retry the loss.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_common_config.rs (L117-135)
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
```

**File:** bridges/snowbridge/primitives/core/src/reward.rs (L45-61)
```rust
/// Error related to paying out relayer rewards.
#[derive(Debug, Encode, Decode)]
pub enum RewardPaymentError {
	/// The XCM to mint the reward on AssetHub could not be sent.
	XcmSendFailure,
	/// The delivery fee to send the XCM could not be charged.
	ChargeFeesFailure,
}

impl From<RewardPaymentError> for DispatchError {
	fn from(e: RewardPaymentError) -> DispatchError {
		match e {
			XcmSendFailure => DispatchError::Other("xcm send failure"),
			ChargeFeesFailure => DispatchError::Other("charge fees error"),
		}
	}
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
