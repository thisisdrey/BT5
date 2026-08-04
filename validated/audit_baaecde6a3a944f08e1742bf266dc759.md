## Finding [1](#0-0) 

### Title
Snowbridge relayer reward can be marked as claimed on BridgeHub while the cross-chain deposit to AssetHub fails, permanently losing the reward - (File: bridges/snowbridge/primitives/core/src/reward.rs)

### Summary
`PayAccountOnLocation::pay_reward` treats successful XCM *delivery* to AssetHub as final proof of payment, but the actual `DepositAsset` (crediting the beneficiary) only happens asynchronously when AssetHub executes that XCM. `do_claim_rewards` in the relayers pallet removes the reward from storage as soon as `pay_reward` returns `Ok(())`, before the deposit is confirmed to have succeeded on the destination chain. This is the same broken invariant as the external report: "settlement" is recorded on the source side before the actual crediting on the receiving side is guaranteed to succeed, so funds can vanish from accounting if the receiving-side step fails.

### Finding Description
`bridges/modules/relayers/src/lib.rs::do_claim_rewards` (lines 263-302) takes the relayer's reward out of `RelayerRewards` storage via `try_mutate_exists`, and calls `T::PaymentProcedure::pay_reward(...)`. Only if `pay_reward` itself returns an `Err` does the mutate revert and preserve the stored reward: [2](#0-1) 

For Snowbridge rewards, `pay_reward` is implemented by `PayAccountOnLocation::pay_reward`: [1](#0-0) 

This builds an XCM program (`UnpaidExecution`, `DescendOrigin`, `UniversalOrigin`, `ReserveAssetDeposited`, `DepositAsset`), validates it, charges delivery fees, and calls `XcmSender::deliver(ticket)`. It returns `Ok(())` as soon as the message is *delivered* to the transport layer — it never waits for or checks the actual execution outcome of `DepositAsset` on AssetHub. `ReserveAssetDeposited` only populates the XCM holding register during execution on AssetHub; it is not chain state. If `DepositAsset` subsequently fails on AssetHub (e.g., the beneficiary is not a "sufficient" holder for the foreign ETH asset and has no native ED, causing the deposit instruction to error), the whole downstream XCM program fails and the reward is never actually credited to the beneficiary account — yet on BridgeHub `do_claim_rewards` has already unconditionally removed the reward entry from storage and emitted `RewardPaid`, since `pay_reward` reported success at the delivery step, not the settlement step.

This mirrors the report's core broken invariant: the source-of-truth ledger (`RelayerRewards` storage / total accounted rewards) is finalized based on "the transfer was initiated/sent" rather than "the transfer was actually credited," so a receiving-side failure (account not eligible/activated for the asset) causes funds to disappear from the relayer's recoverable balance without any corresponding credit, and — unlike the HYPE case, where the manager could withdraw and redeposit — there is no built-in reconciliation path back on BridgeHub; the relayer would need to separately discover and claim any XCM asset-trap on AssetHub, which is not exposed by this reward flow.

### Impact Explanation
A relayer's earned Snowbridge reward can be silently and permanently lost: `RelayerRewards` is cleared, `RewardPaid` is emitted claiming success, but the beneficiary receives nothing if the deposit fails on AssetHub. This is a fund-loss/settlement-integrity bug in the bridge reward accounting path, matching the "deposit disappears from total balance" class of the source report. High impact because relayer compensation directly affects bridge economic security (relayers won't be reliably paid, undermining the incentive to relay Snowbridge messages), and it constitutes duplicate/incorrect settlement bookkeeping (reward marked paid without being paid).

### Likelihood Explanation
Low-to-moderate likelihood: it requires a relayer-chosen or user-chosen beneficiary `Location` on AssetHub that isn't sufficient/eligible to receive the specific foreign asset (e.g., a fresh account with no ED and not otherwise "sufficient" for the bridged ETH asset), which is plausible for `claim_rewards_to` since the beneficiary is caller-supplied via `BridgeRewardBeneficiaries::AssetHubLocation`. No privileged actor, relayer misbehavior, or governance action is needed — a normal unprivileged relayer choosing an arbitrary/incorrect beneficiary triggers the loss.

### Recommendation
Do not treat `XcmSender::deliver` success as final settlement. Options:
- Use a confirmable payment flow (e.g. `PayOverXcm`-style query/callback, as used elsewhere for `PayAccountId32OnChainOverXcm`) so the reward is only removed from `RelayerRewards`/marked paid after execution success is confirmed on AssetHub, or
- Keep the reward record until a receipt/ack XCM confirms deposit success, re-crediting the pending reward on failure, and
- Validate, before sending, that the beneficiary location can plausibly receive the asset (analogous to the report's recommendation to check account activation), reducing the chance of triggering an unrecoverable failure path.

### Proof of Concept
1. Register a Snowbridge reward for `relayer_account` via `BridgeRelayers::register_reward`.
2. Call `claim_rewards_to` with `BridgeRewardBeneficiaries::AssetHubLocation` pointing at a beneficiary `AccountId32` on AssetHub that has zero balance and is not marked "sufficient" for the bridged ETH foreign asset (per `pallet_assets` sufficiency rules), similar to the setup in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_rewards.rs` but without pre-funding/ED for the beneficiary.
3. On BridgeHub, `do_claim_rewards` calls `PayAccountOnLocation::pay_reward`, which only validates+delivers the XCM; it returns `Ok(())`, so BridgeHub removes the reward from `RelayerRewards` and emits `RewardPaid`.
4. On AssetHub, the `DepositAsset` instruction fails because the beneficiary can't hold the foreign asset without meeting sufficiency/ED requirements; no `pallet_assets::Event::Deposited` is emitted for the beneficiary.
5. Result: `RelayerRewards` no longer contains the reward (it is "claimed"), but the beneficiary account never received the funds — the reward is lost from accounting.

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

**File:** bridges/modules/relayers/src/lib.rs (L268-291)
```rust
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
```
