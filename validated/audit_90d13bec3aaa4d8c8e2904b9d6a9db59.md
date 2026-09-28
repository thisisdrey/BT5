### Title
Positions can be liquidated immediately after `Pool.open()` with no grace period for users to restore health - ([File: contracts/Pool.sol])

### Summary
Metronome's `Pauseable.shutdown()` disables every position-management function — `DebtToken.repay`/`repayAll`, `DebtToken.issue`, `DepositToken.deposit`, `DepositToken.withdraw`, `Pool.swap` and `Pool.liquidate` — while `open()` re-enables all of them atomically in a single transaction. `Pool.liquidate` is gated only by `whenNotShutdown` and has no post-reopen grace period, so a liquidator can call `liquidate` in the very first block after `open()`, before affected users have any chance to repay debt or add collateral. The codebase even encodes that `paused` alone never blocks liquidation (`it('should not revert if paused')` in `test/Pool.test.ts`).

### Finding Description
- `Pauseable.shutdown()` sets `_everythingStopped = true` (contracts/utils/Pauseable.sol:113-117). Per `docs/emergency-flags.md`, `everythingStopped` disables `issue`, `leverage`, `repay`, `repayAll`, `liquidate`, `swap`, and `withdraw`; `paused` additionally blocks `deposit`.
- `Pauseable.open()` clears the flag instantly (contracts/utils/Pauseable.sol:97-100). There is no timestamp recording and no grace-period modifier anywhere in the codebase.
- `Pool.liquidate` (contracts/Pool.sol:537-596) is permissionless, `nonReentrant`, and guarded only by `whenNotShutdown`. It checks `debtPositionOf(account_)` at current oracle prices and immediately seizes collateral via `depositToken_.seize`.
- Scenario: governor/guardian calls `shutdown()` for maintenance or during extreme volatility. While frozen, collateral prices move and healthy positions become liquidatable; users cannot `repay`, `deposit`, or `withdraw` during the shutdown. The governor calls `open()`; a liquidation bot that already holds msAssets calls `liquidate` in the same block (or bundles/orders it ahead of the user's `repay`), seizing collateral plus the liquidator incentive. Even though `repay` is a single transaction in Metronome (unlike FlatMoney's two-step announce/execute), the user had zero ability to act during the shutdown and must now compete on ordering against a bot that can pre-position the repayment tokens and monitor the mempool for `open()`.

### Impact Explanation
Users lose collateral plus the liquidation incentive (`_toLiquidator` / `_fee` in `Pool.liquidate`) without any opportunity to cure their position — a direct loss of user funds caused by protocol design, not by user inaction. This is compounded on L2 deployments (Optimism, Base, etc. per `deployments/`) where a keeper can reliably land the first transaction after `open()`.

### Likelihood Explanation
Requires a `shutdown()`/`open()` cycle (privileged trigger, so likelihood is bounded by how often emergency shutdowns occur) plus price movement making positions liquidatable during the freeze — precisely the conditions under which a shutdown would be invoked (high volatility). Once `open()` is called, execution is trivial for any EOA holding synthetic tokens; no flash loan or privileged access is needed for the `liquidate` call itself.

### Recommendation
Record `lastOpenTimestamp` (or `lastUnpauseTimestamp`) in `open()`/`unpause()` and add a modifier on `Pool.liquidate` that reverts while `block.timestamp < lastOpenTimestamp + GRACE_PERIOD`. Alternatively, allow `repay`/`repayAll`/`deposit` during shutdown while keeping `liquidate` disabled until the grace period elapses.

### Proof of Concept
Hardhat outline against the existing `test/Pool.test.ts` fixture:

```ts
// alice has a healthy position: deposited MET, issued msETH
// liquidator holds msETH obtained before shutdown

// 1. Guardian/governor shuts the pool down
await pool.connect(governor).shutdown();   // everythingStopped = true

// 2. Collateral price drops while frozen (oracle update)
await masterOracle.updatePrice(met.address, toUSD('0.95'));

// 3. Alice cannot react — all cures revert with IsShutdown
await expect(msdMET.connect(alice).deposit(1, alice.address)).revertedWithCustomError(pool, 'IsShutdown');
await expect(msEthDebtToken.connect(alice).repay(alice.address, 1)).revertedWithCustomError(pool, 'IsShutdown');
await expect(msdMET.connect(alice).withdraw(1, alice.address)).revertedWithCustomError(pool, 'IsShutdown');

// 4. Governor reopens; liquidator front-runs/races in the same block
await pool.connect(governor).open();
const repayAmt = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address);
await pool.connect(liquidator).liquidate(msEth.address, alice.address, repayAmt, msdMET.address);

// alice's collateral is seized with incentive; she never had a window to act
```

Uncertain aspects: I could not run the tests, and grep output was truncated before showing the exact modifier lines inside `DepositToken.sol`/`DebtToken.sol`, but `docs/emergency-flags.md` explicitly enumerates that `repay`, `repayAll`, `withdraw`, and `deposit` are all disabled under `everythingStopped`/`paused`, which is consistent with the `whenNotShutdown`/`whenNotPaused` usage found.