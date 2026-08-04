### Title
Asset-conversion swap precompile exposes no deadline/expiry bound, allowing signed swap calls to be executed against a stale price at any future block - ([File: substrate/frame/asset-conversion/precompiles/src/lib.rs])

### Summary
The `pallet-asset-conversion` precompile (`IAssetConversion`) exposes `swapExactTokensForTokens` and `swapTokensForExactTokens` to Solidity/`pallet-revive` contracts. Both functions only carry slippage bounds (`amountOutMin` / `amountInMax`) — there is no `deadline` (or any equivalent absolute-time/block bound) parameter at all, unlike the canonical AMM router pattern the external report is about. Any effective time bound therefore falls back entirely to the *outer* extrinsic's generic mortality window (`frame_system::CheckMortality`), which is a coarse, price-agnostic replay-protection mechanism, not a swap-specific deadline. This reproduces exactly the broken invariant from the external report: a swap operation with no caller-supplied absolute deadline can be included/executed far later than the caller intended, against pool state and prices that have since moved.

### Finding Description
`IAssetConversion::swapExactTokensForTokens` and `swapTokensForExactTokens` are defined in the Solidity interface with only `path`, `amountIn`/`amountOut`, `amountOutMin`/`amountInMax`, `sendTo`, `keepAlive`: [1](#0-0) 

The implementation dispatches straight to `pallet_asset_conversion::Pallet::<Runtime>::swap_exact_tokens_for_tokens` / `swap_tokens_for_exact_tokens` with no timestamp/block check anywhere in the precompile call path: [2](#0-1) [3](#0-2) 

Since the precompile is invoked from within contract execution (via `pallet_revive`), the only expiry mechanism that could stop a stale swap from executing is the generic transaction-extension mortality of the *wrapping* extrinsic, `frame_system::CheckMortality`: [4](#0-3) 

`CheckMortality`'s window is configured in blocks of "era" length (commonly a few hundred/thousand blocks, i.e. tens of minutes to hours), and it is a *chain-level replay protection*, not a swap-specific price/time guard: it has no knowledge of `amountOutMin`/`amountInMax` or of how much the pool price is allowed to drift before the swap becomes economically unfavorable to the signer. The precompile's slippage parameters bound *how bad* a swap can be at execution time relative to the amounts encoded at signing time, but they do not bound *when* the swap can be executed — a relayer, meta-tx dispatcher (`pallet-meta-tx`), or delayed dispatcher holding a validly-signed call (e.g. a signed EVM transaction wrapped and submitted by any third party, or a meta-tx per `substrate/frame/meta-tx/src/lib.rs`) can simply wait, within the full mortality window, until the moment that is maximally favorable to itself (e.g. right after a large trade has skewed the pool, or right before a scheduled arbitrage), and only then submit the swap so it settles at the edge of the caller's slippage tolerance instead of at the price that held when the caller actually intended to trade.

This directly mirrors the report's core broken invariant: "the deadline check is bound to values computed at/near execution time (or absent), instead of an absolute caller-chosen deadline enforced against actual inclusion time," making the deadline protection ineffective (here: non-existent for the swap operation itself).

### Impact Explanation
An unprivileged holder of a validly signed swap call (any relayer for a meta-tx, or any party that can trigger execution of a contract holding a pre-signed EVM transaction) can defer execution of `swapExactTokensForTokens`/`swapTokensForExactTokens` to the most economically advantageous block within the full mortality window, extracting value from the signer up to the full slippage tolerance (`amountOutMin`/`amountInMax`) rather than the tolerance the signer actually intended for near-immediate execution. This is a fund-loss primitive against ordinary DEX users of the Asset Hub DEX precompile — the same class of impact the external report is warning about, just with the added severity that here there is *no* dedicated deadline parameter at all (not merely a misused one).

### Likelihood Explanation
Likelihood is moderate: it requires a swap to be routed through a relayed/meta-tx path or a contract holding a signed payload rather than the signer directly submitting and having it mined promptly — a realistic scenario for any wallet/dApp built on the meta-tx or delayed-dispatch patterns already present in this repo (`pallet-meta-tx`, `pallet-whitelist` deferred dispatch). No malicious validator, collator, relayer-with-special-privilege, or governance action is required — only an ordinary third party that has custody of, or visibility into, a signed call before it is included.

### Recommendation
Add an explicit `deadline` (absolute block number or timestamp) parameter to `swapExactTokensForTokens` and `swapTokensForExactTokens` in `IAssetConversion`, and enforce `frame_system::Pallet::<Runtime>::block_number() <= deadline` (or the timestamp equivalent) inside `AssetConversion::swap_exact_tokens_for_tokens` / `swap_tokens_for_exact_tokens` before executing the underlying pallet call, exactly as `pallet-nfts`' atomic-swap feature already does for `deadline`/`DeadlineExpired` (`substrate/frame/nfts/src/features/atomic_swap.rs:191`). This decouples the swap's price-sensitive expiry from the coarse, unrelated `CheckMortality` extrinsic window.

### Proof of Concept
1. User Alice signs a meta-transaction (or an EVM transaction routed through `pallet-revive`) calling `IAssetConversion::swapExactTokensForTokens(path, amountIn, amountOutMin, sendTo, keepAlive)` where `amountOutMin` reflects the pool price at signing time with a modest slippage tolerance, intending near-immediate execution.
2. A relayer (or anyone able to submit/execute the signed payload) withholds submission. The extrinsic's mortality (`CheckMortality`) allows execution any time within its era window (commonly minutes to hours away).
3. During that window, the relayer waits until the pool price has moved unfavorably for Alice (still within her `amountOutMin` bound) — e.g., after a large unrelated trade shifts reserves — then submits the call.
4. `AssetConversion::swap_exact_tokens_for_tokens` (`substrate/frame/asset-conversion/precompiles/src/lib.rs:289-319`) executes unconditionally against current reserves; there is no `deadline` field in the ABI (`substrate/frame/asset-conversion/precompiles/src/lib.rs:56-70`) to reject the late execution, so Alice receives an amount at the edge of her tolerated slippage instead of the amount she would have received at signing time.

### Citations

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L56-86)
```rust
	interface IAssetConversion {
		/// Swap an exact amount of input tokens for as many output tokens as possible.
		/// @param path Ordered list of SCALE-encoded asset identifiers defining the swap route.
		/// @param amountIn Exact amount of the first asset to swap.
		/// @param amountOutMin Minimum acceptable amount of the last asset to receive.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountOut The amount of output tokens received.
		function swapExactTokensForTokens(
			bytes[] calldata path,
			uint256 amountIn,
			uint256 amountOutMin,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountOut);

		/// Swap tokens to receive an exact amount of output tokens.
		/// @param path Ordered list of SCALE-encoded asset identifiers defining the swap route.
		/// @param amountOut Exact amount of the last asset to receive.
		/// @param amountInMax Maximum acceptable amount of the first asset to spend.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountIn The amount of input tokens spent.
		function swapTokensForExactTokens(
			bytes[] calldata path,
			uint256 amountOut,
			uint256 amountInMax,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountIn);

```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L289-319)
```rust
	fn swap_exact_tokens_for_tokens(
		call: &IAssetConversion::swapExactTokensForTokensCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		let path_len = Self::validated_path_len(&call.path)?;
		env.charge(
			<Runtime as pallet_asset_conversion::Config>::WeightInfo::swap_exact_tokens_for_tokens(
				path_len,
			),
		)?;
		let path: Vec<_> =
			call.path.iter().map(|e| Self::decode_asset_kind(e)).collect::<Result<_, _>>()?;

		let sender = Self::caller_account_id(env)?;
		let send_to = env.to_account_id(&H160(call.sendTo.0 .0));

		let amount_out = <pallet_asset_conversion::Pallet<Runtime> as Swap<
			<Runtime as frame_system::Config>::AccountId,
		>>::swap_exact_tokens_for_tokens(
			sender,
			path,
			Self::to_balance(call.amountIn)?,
			Some(Self::to_balance(call.amountOutMin)?),
			send_to,
			call.keepAlive,
		)?;

		Ok(IAssetConversion::swapExactTokensForTokensCall::abi_encode_returns(&Self::to_u256(
			amount_out,
		)?))
	}
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L321-351)
```rust
	fn swap_tokens_for_exact_tokens(
		call: &IAssetConversion::swapTokensForExactTokensCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		let path_len = Self::validated_path_len(&call.path)?;
		env.charge(
			<Runtime as pallet_asset_conversion::Config>::WeightInfo::swap_tokens_for_exact_tokens(
				path_len,
			),
		)?;
		let path: Vec<_> =
			call.path.iter().map(|e| Self::decode_asset_kind(e)).collect::<Result<_, _>>()?;

		let sender = Self::caller_account_id(env)?;
		let send_to = env.to_account_id(&H160(call.sendTo.0 .0));

		let amount_in = <pallet_asset_conversion::Pallet<Runtime> as Swap<
			<Runtime as frame_system::Config>::AccountId,
		>>::swap_tokens_for_exact_tokens(
			sender,
			path,
			Self::to_balance(call.amountOut)?,
			Some(Self::to_balance(call.amountInMax)?),
			send_to,
			call.keepAlive,
		)?;

		Ok(IAssetConversion::swapTokensForExactTokensCall::abi_encode_returns(&Self::to_u256(
			amount_in,
		)?))
	}
```

**File:** substrate/frame/system/src/extensions/check_mortality.rs (L60-107)
```rust
impl<T: Config + Send + Sync> TransactionExtension<T::RuntimeCall> for CheckMortality<T> {
	const IDENTIFIER: &'static str = "CheckMortality";
	type Implicit = T::Hash;

	fn implicit(&self) -> Result<Self::Implicit, TransactionValidityError> {
		let current_u64 = <Pallet<T>>::block_number().saturated_into::<u64>();
		let n = self.0.birth(current_u64).saturated_into::<BlockNumberFor<T>>();
		if !<BlockHash<T>>::contains_key(n) {
			Err(InvalidTransaction::AncientBirthBlock.into())
		} else {
			Ok(<Pallet<T>>::block_hash(n))
		}
	}
	type Pre = ();
	type Val = ();

	fn weight(&self, _: &T::RuntimeCall) -> sp_weights::Weight {
		if self.0.is_immortal() {
			// All immortal transactions will always read the hash of the genesis block, so to avoid
			// charging this multiple times in a block we manually set the proof size to 0.
			<T::ExtensionsWeightInfo as super::WeightInfo>::check_mortality_immortal_transaction()
				.set_proof_size(0)
		} else {
			<T::ExtensionsWeightInfo as super::WeightInfo>::check_mortality_mortal_transaction()
		}
	}

	fn validate(
		&self,
		origin: <T as Config>::RuntimeOrigin,
		_call: &T::RuntimeCall,
		_info: &DispatchInfoOf<T::RuntimeCall>,
		_len: usize,
		_self_implicit: Self::Implicit,
		_inherited_implication: &impl Encode,
		_source: TransactionSource,
	) -> ValidateResult<Self::Val, T::RuntimeCall> {
		let current_u64 = <Pallet<T>>::block_number().saturated_into::<u64>();
		let valid_till = self.0.death(current_u64);
		Ok((
			ValidTransaction {
				longevity: valid_till.saturating_sub(current_u64),
				..Default::default()
			},
			(),
			origin,
		))
	}
```
