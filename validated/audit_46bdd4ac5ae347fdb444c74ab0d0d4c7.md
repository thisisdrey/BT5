### Title
Unbacked relayer reward via `AddTip::add_tip` lets any signed account inflate Snowbridge relayer payouts before funds backing them are verified - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
The Particle report's core broken invariant is: a caller-supplied value that determines a settlement amount is accepted and used to pay out value without the protocol verifying that the value is actually backed by real, escrowed funds. The local analog is Snowbridge's inbound-queue-v2 tip mechanism: `Pallet::<T>::add_tip` unconditionally increases a `Tips` storage entry for any not-yet-processed nonce, with no corresponding withdrawal of currency from the caller. That attacker-controlled tip is later added to the verified `relayer_fee` and passed straight into `RewardLedger::register_reward`, which only performs bookkeeping (crediting a claimable ledger), not an actual transfer. The real transfer happens later, on `claim_rewards`, from a shared sovereign "rewards account" — meaning the tip can create reward liability that was never funded.

### Finding Description
`process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` (lines 214-245) computes: [1](#0-0) 
```
let tip = Tips::<T>::take(nonce).unwrap_or_default();
let total_tip = relayer_fee.saturating_add(tip);
if total_tip > 0 {
    T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
}
```
`relayer_fee` comes from a verified Ethereum event log (backed by real locked Ether on Ethereum, per the module docs), but `tip` comes from the `Tips` storage map, which is populated exclusively by the `AddTip` trait implementation: [2](#0-1) 
```
impl<T: Config> AddTip for Pallet<T> {
    fn add_tip(nonce: u64, amount: u128) -> Result<(), AddTipError> {
        ensure!(amount > 0, AddTipError::AmountZero);
        ensure!(!Nonce::<T>::get(nonce.into()), AddTipError::NonceConsumed);
        Tips::<T>::mutate(nonce, |tip| {
            *tip = Some(tip.unwrap_or_default().saturating_add(amount));
        });
        return Ok(());
    }
}
```
This function only checks that `amount > 0` and that the nonce hasn't been consumed yet — it does **not** withdraw any balance from a caller, does not check any origin-linked deposit, and places no ceiling on `amount`. It is exposed to the runtime as a callable extrinsic surface via the `system-v2`/`system-frontend` pallets (multiple call sites reference `add_tip`), meaning any signed account able to reach that public entrypoint can inflate the `Tips` value for a pending nonce arbitrarily.

Once inflated, `register_reward` (via `RewardLedger`) merely increments a claimable balance in the relayer-rewards ledger — it is pure bookkeeping, not a transfer: [3](#0-2) 
The actual money movement only happens later, when `claim_rewards` invokes `PaymentProcedure::pay_reward`, which performs a real `fungible::Mutate::transfer` from a **shared sovereign "rewards account"** to the beneficiary: [4](#0-3) 
```
fn pay_reward(...) -> Result<(), Self::Error> {
    T::transfer(
        &Self::rewards_account(reward_kind),
        &beneficiary.into(),
        reward.into(),
        Preservation::Expendable,
    ).map(drop)
}
```
Because the `Tips` value that feeds `total_tip` was never validated against, or funded by, any deposit from the account calling `add_tip`, the reward-ledger credit created for the relayer is not backed by any inflow into the rewards sovereign account. This is precisely the analog of the Particle exploit: an unprivileged caller supplies a value (`amount`/`params.data`) that the protocol trusts to determine a payout, without validating that the payout is backed by matching funds actually received.

### Impact Explanation
An unprivileged, signed account can inflate `Tips[nonce]` for any pending (unconsumed) nonce to an arbitrary `u128` value. When that message is eventually processed (by any relayer, including the attacker acting as their own relayer for a message they control/submit), `total_tip` — attacker-controlled and unbacked — is registered as a claimable reward. On `claim_rewards`, this is paid out of the shared sovereign rewards account funded by legitimate bridge users/parachains, draining value that was never deposited for this purpose. This is theft/unbacked-mint of bridge reward funds and can degrade or drain the reward pool used to incentivize honest relaying, directly impacting bridge processing liveness/incentives — matching the "theft or unbacked mint" and "duplicate/incorrect settlement" impact categories.

### Likelihood Explanation
The path requires only a signed extrinsic call to the public `add_tip` entrypoint plus submitting/relaying one otherwise-valid inbound message — no validator, relayer, governance, or admin privilege is needed. The `AddTip::add_tip` implementation itself performs no balance check or transfer, so nothing in `pallet-inbound-queue-v2` stops an arbitrarily large, unfunded tip from being registered as reward liability.

### Recommendation
`add_tip` must require the caller to actually escrow/transfer the tip amount (e.g., withdraw from the caller into the same sovereign rewards account, or hold it in a dedicated tip-escrow storage item) before the value is permitted to flow into `register_reward`. Alternatively, `register_reward` should only ever credit amounts that are provably backed (e.g., cap `total_tip` to `relayer_fee` plus tips whose backing deposit is verified), and `claim_rewards`'s `pay_reward` should fail closed (not just error) if the rewards account balance is insufficient rather than silently succeeding on an already-registered but unbacked ledger credit.

### Proof of Concept
1. Attacker observes a pending, unprocessed inbound message with `nonce = N` (before `Nonce::<T>::set(N)` is called in `process_message`).
2. Attacker calls the exposed `add_tip(N, u128::MAX)` extrinsic (surfaced through `system-v2`/`system-frontend`) with no funds withdrawn.
3. `Tips::<T>::mutate(N, ...)` stores the inflated tip, `AddTipError` checks pass since the nonce is not yet consumed.
4. Attacker (or anyone) submits the proof for message `N` via `submit`, causing `process_message` to compute `total_tip = relayer_fee + u128::MAX-ish` and call `T::RewardPayment::register_reward(&relayer, ..., total_tip)`.
5. The relayer (attacker's own account) calls `claim_rewards`, triggering `PayRewardFromAccount::pay_reward`, which transfers the inflated amount out of the shared rewards sovereign account, draining funds that were never deposited to back this specific reward.

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

**File:** bridges/modules/relayers/src/lib.rs (L107-119)
```rust
	#[pallet::call]
	impl<T: Config<I>, I: 'static> Pallet<T, I>
	where
		BeneficiaryOf<T, I>: From<<T as frame_system::Config>::AccountId>,
	{
		/// Claim accumulated rewards.
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::claim_rewards())]
		pub fn claim_rewards(origin: OriginFor<T>, reward_kind: T::Reward) -> DispatchResult {
			let relayer = ensure_signed(origin)?;

			Self::do_claim_rewards(relayer.clone(), reward_kind, relayer.into())
		}
```

**File:** bridges/primitives/relayers/src/lib.rs (L175-188)
```rust
	fn pay_reward(
		_: &Relayer,
		reward_kind: RewardsAccountParams<LaneId>,
		reward: RewardBalance,
		beneficiary: Self::Beneficiary,
	) -> Result<(), Self::Error> {
		T::transfer(
			&Self::rewards_account(reward_kind),
			&beneficiary.into(),
			reward.into(),
			Preservation::Expendable,
		)
		.map(drop)
	}
```
