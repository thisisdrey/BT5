This confirms the finding. `mint`/`redeem` in `substrate/frame/psm/src/lib.rs` only convert between internal and external asset amounts via decimals scaling (`external_to_internal`/`internal_to_external`) — there is no price oracle or market-value check anywhere in the swap path. The only defense against an approved external asset losing value is the manually-toggled `CircuitBreakerLevel` (`AllEnabled`/`MintingDisabled`/`AllDisabled`), which requires the instance's `emergency_admin` to notice and react (`can_set_circuit_breaker`). This is the direct structural analog of the Salty.IO USDS finding: the protocol assumes collateral (external asset) value tracks the internal stablecoin 1:1, with no autonomous mechanism to detect and respond to a real-market depeg/crash of that external asset before economic loss occurs.

### Title
PSM mints internal stablecoin 1:1 against a depegged/crashed external asset with no autonomous price check or de-risking mechanism - (File: `substrate/frame/psm/src/lib.rs`)

### Summary
`pallet-psm`'s `mint`/`redeem` extrinsics convert between the internal stablecoin and an approved external asset using only decimal scaling, with no oracle or market-price validation. If an approved external asset's real market value drops below par (a stablecoin depeg or crash), the pallet keeps treating it as worth 1:1 until a privileged `emergency_admin` manually flips the per-asset `CircuitBreakerLevel`. There is no autonomous, permissionless mechanism to detect the depeg and pause/de-risk the instance, mirroring the Salty.IO finding where liquidation could not keep pace with a market crash and no autonomous "sell collateral" fallback existed.

### Finding Description
`ExternalAssetInfo` stores only `status` (circuit breaker) and a decimals snapshot — no price feed reference. [1](#0-0) 
`mint` only checks the circuit breaker state, decimal conversion, fee, and debt ceilings before crediting the user 1:1 (in internal-asset terms) and depositing the external asset into the reserve — no external price check exists. [2](#0-1) 
Circuit breaker changes are gated behind `PsmManagerLevel::can_set_circuit_breaker`, which only `Full` or `Emergency` admins can invoke — there is no autonomous on-chain trigger tied to real collateral value. [3](#0-2) 
Because minting is permissionless (any signed account) and unconditioned on price, any user can deposit a depegged/crashing external asset at full nominal (1:1-scaled) value and receive freshly minted internal stablecoin, while the reserve now holds an asset worth less than the debt it is meant to back — this is the same "collateral crashes faster than the protocol reacts" invariant break as the seed report, but expressed as an actively exploitable mint path rather than a passive risk.

### Impact Explanation
This directly threatens "theft or unbacked mint" from the impact gate: the internal stablecoin's aggregate `PsmDebt`/`max_debt` accounting assumes 1:1 backing, but nothing enforces that the external asset actually holds $1 of value at mint time. A sustained depeg of any approved external asset silently undercollateralizes every PSM instance using it, and remediation depends entirely on a privileged admin acting in time — exactly the "absence of autonomous mechanism" gap called out in the seed report.

### Likelihood Explanation
Likelihood is tied to real-world market conditions for whichever external assets a runtime approves (e.g., a USDC/USDT-style asset). No malicious peer, validator, collator, or admin is required — an ordinary user simply needs to observe an external asset trading below par and call the permissionless `mint` extrinsic before an admin reacts via the manual circuit breaker.

### Recommendation
Add price-bound validation to `mint`/`redeem` (e.g., require external oracle attestation of near-par value, or an automatic circuit-breaker trip driven by an on-chain price feed) so that minting halts autonomously once an external asset deviates from par by more than a configured tolerance, rather than depending solely on manual admin intervention.

### Proof of Concept
1. Runtime approves `external_asset` (e.g., a stablecoin) on a PSM instance via `add_external_asset`, with `CircuitBreakerLevel::AllEnabled`.
2. `external_asset` depegs on the open market to, say, $0.80 while remaining `AllEnabled` on-chain (no oracle, no automatic pause).
3. Attacker buys a large amount of the depegged `external_asset` cheaply off-chain/on a DEX.
4. Attacker calls `Psm::mint(origin, internal_asset, external_asset, external_amount, max_fee)`; the pallet only checks circuit breaker status, decimals, fee ceiling, and debt ceilings (`substrate/frame/psm/src/lib.rs` lines 708-741) — no price check — and mints internal stablecoin at full nominal 1:1 value.
5. Attacker immediately redeems or sells the freshly minted internal stablecoin at its (still-pegged) market price, realizing profit equal to the depeg discount, while the PSM reserve is left holding under-valued collateral against its recorded debt.
6. Repeat until `emergency_admin` notices and manually sets the circuit breaker — during which window the internal stablecoin's true backing degrades with no autonomous stop.

### Citations

**File:** substrate/frame/psm/src/lib.rs (L224-228)
```rust
		/// Whether this level allows modifying the circuit breaker status.
		/// Both Full and Emergency levels can set circuit breaker.
		pub const fn can_set_circuit_breaker(&self) -> bool {
			matches!(self, PsmManagerLevel::Full | PsmManagerLevel::Emergency)
		}
```

**File:** substrate/frame/psm/src/lib.rs (L312-330)
```rust
	/// On-chain record of an external asset approved on a PSM instance.
	#[derive(
		Encode,
		Decode,
		DecodeWithMemTracking,
		MaxEncodedLen,
		TypeInfo,
		Clone,
		Copy,
		PartialEq,
		Eq,
		Debug,
	)]
	pub struct ExternalAssetInfo {
		/// Per-external circuit breaker status.
		pub status: CircuitBreakerLevel,
		/// Snapshot of the external asset's decimals at registration time.
		pub decimals: u8,
	}
```

**File:** substrate/frame/psm/src/lib.rs (L708-741)
```rust
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let effective_external =
				Self::internal_to_external(internal_equivalent, ext_decimals, internal_decimals)?;

			let fee_rate = MintingFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_equivalent);
			let internal_to_user = internal_equivalent.saturating_sub(fee);

			let current_total_psm_debt = Self::total_psm_debt(&internal_asset);
			ensure!(
				current_total_psm_debt.saturating_add(internal_equivalent) <= info.max_debt,
				Error::<T>::ExceedsMaxPsmDebt
			);

			let current_debt = PsmDebt::<T>::get(&internal_asset, &external_asset);
			let max_debt = Self::max_asset_debt(&internal_asset, &external_asset, &info);
			let new_debt = current_debt.saturating_add(internal_equivalent);
			ensure!(new_debt <= max_debt, Error::<T>::ExceedsMaxPsmDebt);
```
