## Title
Snowbridge relayer rewards become permanently irretrievable when `claim_rewards_to` deducts the reward on BridgeHub before the AssetHub-side `DepositAsset` to the beneficiary is confirmed - (`bridges/modules/relayers/src/lib.rs`, `bridges/snowbridge/primitives/core/src/reward.rs`)

### Summary
The core broken invariant in the seed report is: a "burn" of the receiving identity (NFT owner set to `address(0)`) happens *before* the payout is attempted, and the payout is irreversibly recorded as done (or the funds are pulled) even though the transfer to the destination can never succeed, leaving the reward stuck forever. The same "debit-before-confirmed-credit" pattern exists in the Snowbridge relayer reward claim flow: `pallet-bridge-relayers::claim_rewards_to` deletes the relayer's reward entry on BridgeHub as soon as the payment procedure returns `Ok(())`, but for Snowbridge rewards, "success" only means an XCM message was *sent* toward AssetHub - not that the beneficiary actually received the minted asset.

### Finding Description
`Pallet::do_claim_rewards` atomically takes the stored reward and calls the configured `PaymentProcedure::pay_reward`; only if that call errors does the mutation roll back: [1](#0-0) 

For `BridgeReward::Snowbridge`, the payment procedure is `PayAccountOnLocation`, whose `pay_reward` builds an XCM containing `ReserveAssetDeposited` + `DepositAsset { beneficiary, .. }`, validates it, charges delivery fees, and calls `XcmSender::deliver(ticket)`: [2](#0-1) 

`deliver` only confirms that the message was accepted for delivery to AssetHub - it says nothing about whether the `DepositAsset` instruction will actually succeed once executed there. Because `pay_reward` returns `Ok(())` as soon as `deliver` succeeds, `do_claim_rewards` commits the storage mutation removing the relayer's `RelayerRewards` entry and emits `RewardPaid`: [3](#0-2) 

If the caller-supplied `beneficiary` `Location` on AssetHub cannot receive the deposit (e.g., account below the foreign-asset's minimum/existential balance, an account that doesn't exist and the asset requires sufficient existence, or any other execution failure of `DepositAsset` inside the remote XCM), the XCM aborts on AssetHub, and the withheld ETH-derived asset is trapped (recoverable only via a privileged `claim_assets`/governance action targeting the exact trapped-asset key - not by the relayer). Meanwhile BridgeHub has already deleted the reward record and told the relayer, via the `RewardPaid` event, that payment succeeded. The relayer has no way to re-claim: `RelayerRewards::<T,I>::try_mutate_exists` already removed the entry, so a second `claim_rewards_to` call fails with `NoRewardForRelayer`.

This is structurally identical to the seed bug: the "ownership"/beneficiary state that gates final settlement (NFT owner vs. XCM beneficiary account) can be in a state that makes the final transfer fail, yet the pallet has already treated the reward as consumed, so the value becomes permanently stuck - just on the wrong side of a cross-chain boundary instead of inside one contract.

### Impact Explanation
This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" - `RelayerRewards` state advances (is deleted) based only on successful *dispatch* of the XCM, not on successful *settlement* at the destination. The result is permanent loss of the relayer's WETH reward with no recovery path through the intended user-facing flow, which matches the explicitly accepted impact class "permanent user-fund or bridge-state lock."

### Likelihood Explanation
The trigger is fully within reach of an ordinary, unprivileged relayer: `claim_rewards_to` is a public extrinsic and the `beneficiary: BeneficiaryOf<T, I>` (a `VersionedLocation`/`AccountId32` on AssetHub) is caller-supplied. A relayer who specifies a beneficiary account that is unfunded/below the foreign-asset minimum balance on AssetHub (a routine mistake, not an attack requiring any privileged actor, malicious relayer, or governance abuse) will have their reward silently and permanently destroyed while BridgeHub still reports `RewardPaid`.

### Recommendation
Do not remove/consume the `RelayerRewards` entry (or otherwise treat the claim as final) until settlement on the destination chain is confirmed - e.g., require an explicit delivery/execution acknowledgment (return receipt) from AssetHub before finalizing the claim on BridgeHub, or make the removal reversible/re-claimable if the remote `DepositAsset` fails (mirroring the recommended NFT clawback: fall back to crediting a recoverable account, e.g., the relayer's local BridgeHub account or a treasury, if the remote deposit cannot be confirmed).

### Proof of Concept
1. Register a Snowbridge reward for a relayer via `BridgeRelayers::register_reward`.
2. Call `claim_rewards_to(origin = relayer, reward_kind = Snowbridge, beneficiary = AssetHubLocation(location_of_unfunded_or_invalid_account))`.
3. On BridgeHub: `do_claim_rewards` calls `PayAccountOnLocation::pay_reward`, which only validates and delivers the XCM ticket (`bridges/snowbridge/primitives/core/src/reward.rs:145-148`); it returns `Ok(())`, so `RelayerRewards` entry is deleted and `RewardPaid` is emitted.
4. On AssetHub: the received XCM's `DepositAsset { beneficiary, .. }` fails because the beneficiary account cannot satisfy the ED/minimum-balance requirement for the foreign WETH asset; the asset is trapped instead of credited (see the working-case test `claim_rewards_works` at `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs:25-103`, which only asserts success for a *funded* beneficiary - no test exercises the unfunded/failing-deposit path).
5. Relayer calls `claim_rewards_to` again for the same reward and gets `Error::NoRewardForRelayer` - the reward is gone from BridgeHub and the corresponding asset is stuck in AssetHub's trap storage, unrecoverable by the relayer.

### Citations

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
