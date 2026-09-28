### Title
Dust-deposit griefing fills `MAX_TOKENS_PER_USER` slots of `feeCollector`, reverting `Pool.liquidate` and `DepositToken.deposit` — ([File: contracts/Pool.sol](contracts/Pool.sol) / [contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` on the *recipient* account whenever a `DepositToken` balance goes 0 → non-zero, and `DepositToken.deposit` lets anyone mint deposit-token balances `onBehalfOf_` an arbitrary address. An unprivileged attacker can deposit dust of every registered `DepositToken` on behalf of `feeCollector` (and/or a victim user). Once that account's combined `debtTokensOfAccount + depositTokensOfAccount` list reaches 30, every `seize`/`_transfer`/`_mint` that would add a *new* token to that account reverts with `UserReachedMaxTokens`. Since `Pool.liquidate` unconditionally does `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)` when the liquidation fee is non-zero, all liquidations of collaterals not already held by `feeCollector` revert — a liveness/DoS of the liquidation path, plus DoS of `deposit`/`withdraw` fee mints (`_mint(feeCollector, _fee)`).

### Finding Description
- `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` (`contracts/DepositToken.sol:211-237`) mints `_deposited` to `onBehalfOf_` with no restriction that `onBehalfOf_` be the caller (only `Treasury` is excluded via `TreasuryCanNotDeposit`).
- `DepositToken._mint` and `_transfer` call `pool.addToDepositTokensOfAccount(account)` when the recipient's prior balance is zero (`contracts/DepositToken.sol:486-488`, `518-520`).
- `Pool.addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens(account_)`, which reverts `UserReachedMaxTokens` when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148`, `79`, `216-220`).
- `Pool.liquidate` seizes `_fee` to `feeCollector` whenever `_fee > 0` (`contracts/Pool.sol:581-593`). If `feeCollector` does not already hold that `DepositToken` and is at the cap, `seize` → `_transfer` → `addToDepositTokensOfAccount` reverts, reverting the whole liquidation.
- The same revert blocks `deposit` (`_mint(feeCollector, _fee)` at `DepositToken.sol:231`) and `withdraw`/`flashWithdraw` fee transfers (`_transfer(account, feeCollector, _fee)` at `DepositToken.sol:547`) for any collateral token not already in `feeCollector`'s list.
- The victim-user variant is weaker: filling a user's 30 slots only blocks *that* user from adding new token types, and the user can self-cure by transferring dust out. The `feeCollector` variant is systemic because `feeCollector` is set by `PoolRegistry` and cannot cheaply empty slots unless it is a governable contract that can call `DepositToken.transfer` — and even then, only via governance action.

### Impact Explanation
Liquidations are the solvency mechanism of the pool. While `feeCollector`'s token list is saturated, `liquidate` reverts for every collateral type the collector doesn't already hold, so unhealthy positions cannot be liquidated and bad debt can accumulate — a repeatable crash/hang analog to the InnoDB DoS class (availability impact only). `deposit`/`withdraw` are also bricked for unlisted collaterals as long as deposit/withdraw fees are non-zero. Attacker cost is dust amounts of the underlying collaterals plus gas; there is no slashing or privileged-role requirement.

### Likelihood Explanation
Exploitability is conditional on the number of registered `DebtToken` + `DepositToken` contracts a target account can be forced to hold reaching 30. `MAX_TOKENS_PER_USER` counts the union of both sets (`contracts/Pool.sol:144`). If a deployed pool's combined registered token set is ≥ 30 distinct tokens the attacker can push onto `feeCollector` (deposit tokens via `deposit(1 wei, feeCollector)`; debt tokens only if an issue path supports `onBehalfOf_`, which I could not fully verify in `DebtToken.issue` within available context), the DoS is fully permissionless. If the pool has fewer than ~30 registered assets, the cap cannot be reached via deposits alone and the attack is not viable on that deployment. Note also that `_mint`/`seize` to `feeCollector` only revert for tokens it doesn't already hold, so partially-saturated lists degrade the DoS to a subset of collaterals.

### Recommendation
- In `DepositToken.deposit`, restrict `onBehalfOf_` or exempt `feeCollector`/registry-critical addresses, or
- In `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, skip the `MAX_TOKENS_PER_USER` check (or use a separate higher cap) for `feeCollector` and other protocol contracts, or
- In `Pool.liquidate`/`_withdraw`, pull the fee via a path that cannot be reverted by recipient list limits (e.g., keep seized fees in the Pool/Treasury and let `feeCollector` claim later), and/or
- Make `DepositToken.transfer`/`seize` to `feeCollector` bypass the per-account token-list bookkeeping.

### Proof of Concept
Foundry/Hardhat fork outline:

```solidity
// Fork a chain where the Pool has >= 30 registered DepositToken/DebtToken assets
// (or deploy a local harness registering 30 mock DepositTokens).

IPool pool = IPool(POOL);
address feeCollector = pool._poolRegistry().feeCollector();

// 1) Attacker fills feeCollector's deposit-token list
for (uint i; i < 30; ++i) {
    IDepositToken dt = IDepositToken(pool.getDepositTokens()[i]);
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 1);
    underlying.approve(address(dt), 1);
    // feeCollector is not Treasury, so only the TreasuryCanNotDeposit check is skipped
    dt.deposit(1, feeCollector);   // mints dust, adds dt to feeCollector's list
}
assertEq(
    pool.getDepositTokensOfAccount(feeCollector).length
      + pool.getDebtTokensOfAccount(feeCollector).length,
    30
);

// 2) Any deposit of a collateral not already in feeCollector's list now reverts
IDepositToken newDt = ...; // token #31 not in the list
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
newDt.deposit(amount, alice); // reverts at _mint(feeCollector, _fee) when depositFee > 0

// 3) Liquidation of a collateral not held by feeCollector reverts
// alice has an unhealthy position in `newDt` collateral
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
pool.liquidate(msToken, alice, amountToRepay, newDt); // reverts at seize -> _transfer -> addToDepositTokensOfAccount
```

Caveat: the PoC requires the deployed pool to expose ≥30 distinct tokens that can be added to `feeCollector`'s per-account lists (or a configuration where feeCollector already holds many). On pools below that threshold, this degrades to a per-victim griefing (block a user from new token types, curable by transferring dust out), which is low impact and would not qualify.