Confirmed: neither the pallet's extrinsics nor its Solidity precompile interface include any `deadline`/expiry parameter or timestamp check for swaps.

### Title
Swap Extrinsics and Precompile in `pallet-asset-conversion` Lack Deadline Enforcement, Allowing Stale-Price Execution - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` implements an on-chain constant-product DEX (`AssetHub DEX`). Its public dispatchables `swap_exact_tokens_for_tokens` and `swap_tokens_for_exact_tokens`, together with the Solidity-facing precompile `IAssetConversion.swapExactTokensForTokens` / `swapTokensForExactTokens`, only protect against price movement via `amount_out_min` / `amount_in_max` slippage bounds. There is no `deadline` parameter and no timestamp/block-number check anywhere in the swap path, mirroring exactly the bug class described in the external report (`GluexRouter.swap()` executing without a deadline check).

### Finding Description
The extrinsics are defined without any expiry field: [1](#0-0) [2](#0-1) 

Both eventually call `do_swap_exact_tokens_for_tokens` / `do_swap_tokens_for_exact_tokens`, which only validate the path and the min/max amount bound before executing the swap against the pool's current reserves: [3](#0-2) 

A `grep` across the whole `substrate/frame/asset-conversion/**` tree for the string `deadline` returns zero matches, confirming no expiry check exists anywhere in the pallet, including the `Swap`/`SwapCredit` traits: [4](#0-3) 

The Solidity-facing precompile interface (used by EVM contracts via `pallet-revive`) exposes the exact same two functions with the exact same parameter set (`path`, amount, min/max, `sendTo`, `keepAlive`) and likewise has no `deadline` field: [5](#0-4) 

Because only `amount_out_min`/`amount_in_max` guard the trade, a submitted swap transaction remains valid and executable indefinitely (subject only to the generic transaction-mortality window applied by `frame_system::CheckMortality`, which bounds inclusion in blocks but is not price-aware and is typically set to a long default). If a relayer, block-proposer, or the submitter's own wallet delays inclusion (e.g. congested mempool, intentional withholding, or MEV-style reordering), the swap will still execute at whatever price is in the pool at that later block — even though the user only intended to trade based on the pool state and slippage tolerance calculated at submission time.

### Impact Explanation
This directly parallels the "H-03" bug class: a swap can be executed at a price the user never consented to, because the sole protection (`amount_out_min`/`amount_in_max`) only bounds output/input amount relative to reserves *at execution time*, not relative to *time elapsed since signing*. An adversarial block producer or a delayed relay can hold a signed swap transaction and release it once the pool has drifted (via other swaps) to a boundary condition that still satisfies the slippage bound but is maximally unfavorable to the user, effectively extracting value (sandwich/stale-price attacks) without needing any privileged role, malicious validator collusion beyond normal transaction-ordering power, or off-chain infrastructure compromise. This is a public, unprivileged-attacker-reachable value-extraction path against real user funds moving through the AssetHub DEX.

### Likelihood Explanation
High likelihood: `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` and their precompile equivalents are the primary public entry points for on-chain swaps on Asset Hub, callable by any signed account (`ensure_signed(origin)?`) with no special permissions. Any actor with normal block-authoring or transaction-ordering influence (which is a routine, expected capability in the protocol, not "malicious validator" collusion) can exploit the missing deadline to delay execution to their advantage; the mortality window from the generic transaction extension does not address this because it is not price/time-window-aware and defaults are commonly long relative to price-sensitive trading conditions.

### Recommendation
Add an explicit `deadline: BlockNumberFor<T>` (or timestamp) parameter to `swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens` (and the corresponding `Swap`/`SwapCredit` trait methods and the Solidity precompile interface), and enforce it at the top of `do_swap_exact_tokens_for_tokens`/`do_swap_tokens_for_exact_tokens`:
```rust
ensure!(
    frame_system::Pallet::<T>::block_number() <= deadline,
    Error::<T>::DeadlineExpired
);
```
This closes the gap between "acceptable slippage" and "acceptable staleness," matching standard AMM router designs (e.g. Uniswap V2/V3 routers) that the external report references.

### Proof of Concept
1. User Alice wants to swap `token_1` for `token_2`, computes `amount_out_min` based on the current pool reserves, and signs `swap_exact_tokens_for_tokens(path, amount_in, amount_out_min, alice, true)`.
2. The transaction propagates to the mempool, but a block-producing collator with ordering influence (or a slow relay) withholds it for many blocks while other swaps move the pool price close to (but still above) Alice's `amount_out_min` threshold.
3. When the block producer eventually includes Alice's transaction, `do_swap_exact_tokens_for_tokens` (`substrate/frame/asset-conversion/src/lib.rs:980-1014`) checks only `amount_out >= amount_out_min` — which still passes — and executes the swap at the now-worse price.
4. Alice receives the minimum amount she specified, but far less favorable than what she would have received had the swap executed promptly; there is no on-chain mechanism (deadline) that would have caused the transaction to revert instead, because none exists in the pallet or precompile.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L527-545)
```rust
		pub fn swap_exact_tokens_for_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_in: T::Balance,
			amount_out_min: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_exact_tokens_for_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_in,
				Some(amount_out_min),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L555-573)
```rust
		pub fn swap_tokens_for_exact_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_out: T::Balance,
			amount_in_max: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_tokens_for_exact_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_out,
				Some(amount_in_max),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L980-1014)
```rust
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> Result<T::Balance, DispatchError> {
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}

			Self::swap(&sender, &path, &send_to, keep_alive)?;

			Self::deposit_event(Event::SwapExecuted {
				who: sender,
				send_to,
				amount_in,
				amount_out,
				path,
			});
			Ok(amount_out)
		}
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L43-69)
```rust
	fn swap_exact_tokens_for_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_in: Self::Balance,
		amount_out_min: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;

	/// Take the `path[0]` asset and swap some amount for `amount_out` of the `path[last]`. If an
	/// `amount_in_max` is specified, it will return an error if acquiring `amount_out` would be
	/// too costly.
	///
	/// Withdraws `path[0]` asset from `sender`, deposits `path[last]` asset to `send_to`,
	/// respecting `keep_alive`.
	///
	/// If successful returns the amount of the `path[0]` taken to provide `path[last]`.
	///
	/// This operation is expected to be atomic.
	fn swap_tokens_for_exact_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_out: Self::Balance,
		amount_in_max: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L56-85)
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
