### Title
Dust-transfer griefing of `feeCollector` fills `MAX_TOKENS_PER_USER` account list, causing protocol-wide DoS of deposits, withdrawals and liquidations - (File: contracts/Pool.sol)

### Summary
`Pool` keeps a per-account `MappedEnumerableSet` of the deposit/debt tokens an account has ever touched, capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`, `:143-148`). `DepositToken._mint`/`_transfer` unconditionally call `pool.addToDepositTokensOfAccount(recipient)` when the recipient's balance goes 0 → >0 (`contracts/DepositToken.sol:486-488`, `:518-520`), and that call reverts `UserReachedMaxTokens` once the recipient's combined debt+deposit set reaches 30 (`contracts/Pool.sol:143-146`). Any EOA can `deposit` dust and `transfer` 1 wei of msdTokens to *any* address — including `pool.feeCollector()` — forcibly filling its set. Once the feeCollector's set is full, every fee-bearing `deposit()`, `withdraw()`, and liquidation fee-split reverts, temporarily freezing user funds and blocking liquidations.

### Finding Description
- `deposit()` mints `_fee` to `pool.feeCollector()` (`contracts/DepositToken.sol:229-232`) and `_withdraw()` transfers `_fee` to the feeCollector via `_transfer` (`contracts/DepositToken.sol:545-548`). Both paths hit `addToDepositTokensOfAccount` when feeCollector's balance of that token is 0.
- `seize(from, to, amount)` → `_transfer` (`contracts/DepositToken.sol:343-345`) is used by `Pool.liquidate` for the seized collateral and fee split, so fee transfers to feeCollector in liquidations revert as well.
- Attack: for each listed `DepositToken` i, attacker calls `deposit(dust_i, attacker)` then `transfer(feeCollector, 1)`. After 30 distinct tokens, `getDepositTokensOfAccount(feeCollector).length == 30`. From then on any `_transfer`/`_mint` giving feeCollector a token it doesn't already hold reverts `UserReachedMaxTokens`, reverting the parent `deposit`/`withdraw`/`liquidate`.

### Impact Explanation
Temporary freezing of funds and liquidation liveness failure: all deposits and withdrawals that carry a nonzero fee revert while the set is saturated, and liquidations whose fee leg targets a new token revert, leaving unhealthy positions un-liquidatable during the window. Recovery requires the feeCollector (or governor) to manually transfer dust out — users cannot self-recover because the revert is inside protocol transfers.

### Likelihood Explanation
Requires (a) nonzero deposit/withdraw/liquidation fees and (b) enough listed deposit tokens for the attacker to fill 30 combined slots on the feeCollector (debt tokens are not freely transferable, so the filler must come from deposit tokens; pools with fewer listed collaterals limit the attack to only a proportional number of slots and may not reach the cap). Cost is only dust of each underlying plus gas. No privileged role, oracle manipulation, or trusted party is needed — `deposit` and `transfer` are public and unrestricted by pause flags or reentrancy guards.

### Recommendation
- Skip `addToDepositTokensOfAccount` for privileged sink addresses (feeCollector, treasury, SmartFarmingManager), or cap-checked add only for accounts that can open debt positions.
- Alternatively, make `addToDepositTokensOfAccount` non-reverting for fee transfers (emit/event-only bookkeeping) or sweep dust allowance so the cap can never be reached by unsolicited transfers.

### Proof of Concept
Hardhat fork sketch:

```ts
// contracts/Pool.sol cap = 30; attacker fills feeCollector's account set
const feeCollector = await pool.feeCollector()
for (const dt of await pool.getDepositTokens()) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.approve(dt.address, dust)
  await dt.deposit(dust, attacker.address)          // mint msdToken to attacker
  await dt.transfer(feeCollector, 1)                // fills feeCollector's set
}
expect((await pool.getDepositTokensOfAccount(feeCollector)).length).to.eq(30)

// victim flows now revert
await expect(dtVictim.deposit(amount, victim.address)).revertedWithCustomError(pool, 'UserReachedMaxTokens')
await expect(dtVictim.withdraw(amount, victim.address)).revertedWithCustomError(pool, 'UserReachedMaxTokens')
await expect(pool.liquidate(victim, syn, debtToken, dtVictim, amount)).revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Caveat: I could not verify the exact fee-split lines inside `Pool.liquidate` or the current fee configuration (`depositFee`/`withdrawFee`/`protocolLiquidationFee` > 0) or the number of listed deposit tokens on the deployed pools within this session — the attack's reach depends on those. If fees are zero or the pool lists few collaterals, the analog degrades to a per-victim griefing (same mechanism targeting a user to block new borrows/deposits), which is weaker and recoverable.