Found the analog: `PayAccountOnLocation::pay_reward` in `bridges/snowbridge/primitives/core/src/reward.rs` charges the delivery fee from the *relayer's* own account (via `XcmExecutor::charge_fees(relayer.clone(), fee)`), but constructs the `ReserveAssetDeposited`/`DepositAsset` instructions using the full requested `reward` amount for the beneficiary, and unconditionally reports success (removing the pending reward from storage) once the XCM is *sent*, not once it is actually *executed and deposited* on AssetHub.

### Title
Reward accounting assumes full requested amount is minted on destination without verifying actual XCM-execution outcome - ([File: bridges/snowbridge/primitives/core/src/reward.rs])

### Summary
`pallet_bridge_relayers::do_claim_rewards` removes the relayer's `RelayerRewards` entry and emits `RewardPaid` with the full `reward_balance` as soon as `T::PaymentProcedure::pay_reward` returns `Ok(())`. For Snowbridge rewards, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`, which only confirms that the XCM message was *sent* (`XcmSender::deliver(ticket)` succeeded) — it never confirms that the `DepositAsset` instruction on AssetHub actually completed for the full `reward` amount.

### Finding Description
`do_claim_rewards` in [1](#0-0)  takes the stored `reward_balance`, calls `PaymentProcedure::pay_reward`, and on `Ok(())` permanently deletes the reward entry and emits `RewardPaid { reward_balance, .. }` for the full amount — this is irreversible bookkeeping that assumes the beneficiary actually received `reward_balance`.

For the Snowbridge reward kind, `pay_reward` is `PayAccountOnLocation::pay_reward` at [2](#0-1)  which builds an XCM with `ReserveAssetDeposited(assets.into())` and `DepositAsset { assets: AllCounted(1).into(), beneficiary }` for the entire `reward` amount, executed as `UnpaidExecution`. The function returns `Ok(())` as soon as `XcmSender::deliver(ticket)` succeeds — it does not wait for, or otherwise validate, the actual execution result on the destination (AssetHub). If the XCM fails to execute there (e.g. `DepositAsset` fails because the beneficiary account doesn't exist and can't be created with the deposited amount, asset registration is missing, weight/instruction limits are hit, or any other execution-time failure occurs), the source chain (BridgeHub) has already deleted the `RelayerRewards` entry and emitted `RewardPaid`, permanently losing the relayer's claim to that reward with no on-chain record left to retry or recover it. This mirrors the reported bug class exactly: the code assumes that the *requested* amount (`reward`) is the *actually settled* amount, when the underlying transfer mechanism (cross-consensus XCM delivery+execution, analogous to the fee-deducting `borrow`) can silently under-deliver or fail to deliver at all, and the bookkeeping (`RelayerRewards` storage / `PaidOut`-style event) is settled before the actual settlement is confirmed.

### Impact Explanation
A relayer's legitimately earned reward can be permanently and irrecoverably burned/lost from the protocol's accounting: `RelayerRewards` is cleared and the event says it was paid, but the actual asset deposit on AssetHub may never happen. This is a fund-loss/permanent-lock class issue for the relayer (no way to reclaim the reward) and breaks the "settle exactly once to the rightful beneficiary and amount" invariant, since settlement state advances on send-success rather than confirmed execution-success.

### Likelihood Explanation
This does not require a malicious actor — it can be triggered by ordinary conditions: an AssetHub-side execution failure (e.g., beneficiary account can't be created, insufficient existential deposit for the reward asset, asset not registered/sufficient, or transient AssetHub congestion causing message drop). Because `UnpaidExecution` and reserve-based deposit are used without any confirmation/receipt mechanism, execution failure at destination is a normal failure mode of cross-chain messaging, not an edge case requiring a compromised relayer/validator.

### Recommendation
Do not treat `XcmSender::deliver` success as final settlement. Options: (1) use a receipted/acknowledged delivery mechanism and only clear `RelayerRewards` once destination-side execution success is confirmed (e.g. via a callback/receipt message back to BridgeHub), or (2) keep the `RelayerRewards` entry (or move it to a "pending confirmation" state) until confirmation, allowing retry on failure, instead of eagerly deleting it in `do_claim_rewards` based solely on `pay_reward` returning `Ok(())` for a fire-and-forget XCM send.

### Proof of Concept
1. Relayer accumulates `reward_balance = X` under `BridgeReward::Snowbridge` in `RelayerRewards` (via `register_reward`).
2. Relayer calls `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation(loc)` where `loc` corresponds to a fresh AssetHub account with no existing sufficient balance for the reward's foreign asset, or where the foreign asset is not (yet) sufficient/registered on AssetHub.
3. `BridgeRewardPayer::pay_reward` → `PayAccountOnLocation::pay_reward` builds and sends the XCM; `XcmSender::deliver` succeeds, so `pay_reward` returns `Ok(())`. [3](#0-2) 
4. Back on BridgeHub, `do_claim_rewards` sees `Ok(())`, removes the `RelayerRewards` entry, and emits `RewardPaid { reward_balance: X, .. }`. [4](#0-3) 
5. On AssetHub, the `DepositAsset` instruction fails (e.g., account cannot be created due to ED, or asset deposit fails) — the relayer never actually receives the `X` reward, yet BridgeHub state shows it as fully paid and there is no remaining record to reclaim it.

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
