### Title
Unconditional relayer reward registration mints unbacked wrapped-ETH on AssetHub without verifying backing funds - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
The DeXe bug's core invariant break is: a public execution path advances a "promise to pay" (a proposal marked executable) without verifying that the declared payout amount is actually backed by the value that was sent (`amount == msg.value`), so the payout later reverts and locks user rewards. The Snowbridge inbound-queue-v2 pallet has the same structural weakness: it registers a relayer reward credit and, on claim, mints wrapped-Ether into a beneficiary account via `ReserveAssetDeposited`, based on a `total_tip` value that is never checked against an actually-reserved/locked backing amount at the point of registration.

### Finding Description
In `Pallet::process_message`, after a message is converted to XCM and forwarded, the pallet computes and registers a reward without any solvency check: [1](#0-0) 

`relayer_fee` comes straight from the decoded Ethereum event (`message.relayer_fee`), and `tip` comes from the `Tips` storage map, which is populated by the permissionless `AddTip::add_tip` implementation: [2](#0-1) 

`add_tip` only checks `amount > 0` and that the nonce hasn't been consumed — it performs **no transfer, no reservation, and no verification that any real value was locked to back `amount`** before it is merged into `total_tip` and registered as a claimable reward via `T::RewardPayment::register_reward`.

When the relayer later claims the reward through `pallet_bridge_relayers::claim_rewards_to`, the Snowbridge payment procedure mints the reward directly via XCM `ReserveAssetDeposited` + `DepositAsset` on AssetHub, treating the registered amount as backed Ethereum-locked value: [3](#0-2) 

Nothing in `process_message`, `add_tip`, or `pay_reward` cross-checks that `total_tip` (the "amount" being promised, analogous to DeXe's `amount`) is less than or equal to real value actually locked on the Ethereum side (analogous to DeXe's `msg.value`). This is precisely the missing `amount == msg.value` guard from the original report, transplanted to the credit-then-mint reward flow: a value is registered as owed/mintable without validating it against the value that was actually supplied.

This is corroborated by `prdoc/1.10.0/pr_3761.prdoc`, which documents that inbound message processing (and, by extension, reward registration) is explicitly allowed to proceed even "if the AH sovereign account is depleted and relayer rewards cannot be paid" — confirming the pallet's design intentionally decouples reward bookkeeping from real backing-fund verification at the point of registration: [4](#0-3) 

### Impact Explanation
If `total_tip` can exceed the Ether actually locked/backing the message (e.g. through the unchecked `add_tip` path or a `relayer_fee` field that is not reconciled against `message.value`), the pallet registers a reward that `PayAccountOnLocation::pay_reward` will later try to mint via `ReserveAssetDeposited` on AssetHub. This either (a) results in an unbacked mint of wrapped ETH on AssetHub (fund creation without matching Ethereum-side lock), or (b) causes `claim_rewards_to` to fail with `FailedToPayReward` when the sovereign account can't cover it, which — combined with `try_mutate_exists` semantics in `do_claim_rewards` — permanently strands the relayer's registered reward in a state that can never be successfully claimed if the shortfall is never remedied. Both outcomes map directly onto the Required Impacts: unbacked mint / duplicate or wrong-amount settlement / permanent fund-state lock.

### Likelihood Explanation
`add_tip` is reachable without any privileged origin check in the trait implementation itself (whatever caller invokes it inherits no fund-locking obligation from this code), and `relayer_fee` is attacker-influenced Ethereum-origin data that the pallet fully trusts once Merkle-verified — but the pallet performs no explicit invariant that `relayer_fee + tip <= message.value` (the total Ether actually escrowed for this message) before crediting `total_tip` as payable. This is a directly reachable, low-complexity path through the pallet's normal public dispatch (`submit` → `process_message`) and the tip-adding entry point, requiring no validator, relayer collusion, or governance action beyond normal message submission.

### Recommendation
Before calling `T::RewardPayment::register_reward`, enforce an explicit invariant that `relayer_fee.saturating_add(tip) <= message.value` (or an equivalent locked-ether accounting field carried through the message), mirroring the DeXe fix of requiring `amount == msg.value`. Additionally, `add_tip` should require and record a corresponding real transfer/reservation of funds at the point the tip is added, not merely a storage increment, so that `Tips` can never represent an unbacked promise.

### Proof of Concept
Conceptual reproduction (mirrors the DeXe PoC structure):
1. A permissionless caller invokes `AddTip::add_tip(nonce, amount)` for a pending message nonce, inflating `Tips[nonce]` with no accompanying value transfer — as shown, this function contains no fund-check logic: [2](#0-1) .
2. The corresponding message is submitted and processed via `process_message`, which computes `total_tip = relayer_fee + tip` and unconditionally calls `register_reward`: [1](#0-0) .
3. The relayer calls `claim_rewards_to`; `pay_reward` attempts to mint `total_tip` worth of wrapped ETH via `ReserveAssetDeposited` on AssetHub regardless of whether Ethereum-side value actually backs the inflated tip: [5](#0-4) .
4. Either the mint succeeds unbacked, or it fails against a depleted sovereign account — a scenario the codebase's own PR documentation acknowledges is expected to occur and to be tolerated rather than prevented: [6](#0-5) .

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L248-259)
```rust
	impl<T: Config> AddTip for Pallet<T> {
		fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
			ensure!(amount > 0, AddTipError::AmountZero);
			// If the nonce is already processed, return an error
			ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
			// Otherwise add the tip.
			Tips::<T>::mutate(nonce, |tip| {
				*tip = Some(tip.unwrap_or_default().saturating_add(amount));
			});
			return Ok(());
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

**File:** prdoc/1.10.0/pr_3761.prdoc (L1-8)
```text
title: "Snowbridge: Synchronize from Snowfork repository"

doc:
  - audience: Runtime Dev
    description: |
      This PR improves the beacon client to send the execution header along with the message as proof and removes the verification and storing of all execution headers.
      If the AH sovereign account is depleted and relayer rewards cannot be paid, the message should still be processed.

```
