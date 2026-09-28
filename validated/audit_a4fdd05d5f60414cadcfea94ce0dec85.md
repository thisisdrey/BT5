### Title
No grace period after `Pool` shutdown lets liquidators instantly seize positions that went unhealthy while borrowers were frozen - (File: contracts/utils/Pauseable.sol)

### Summary
Metronome's `Pauseable` two-tier scheme gives the exact bug class from the external report. `shutdown()` sets `_everythingStopped = true`, which blocks `DebtToken.repay()`, `DebtToken.repayAll()`, `DepositToken.deposit()`, `DepositToken.withdraw()`, and `Pool.liquidate()` simultaneously via `whenNotShutdown`. `open()` re-enables all of them atomically in a single transaction, with no grace period. Worse, `open()` leaves `_paused = true`, so `DepositToken.deposit()` stays blocked until a separate `unpause()` call — while `repay` and `liquidate` (both gated only by `whenNotShutdown`, not `whenNotPaused`) resume instantly. A borrower whose position drifted unhealthy during the shutdown can be liquidated in the same block the pool reopens, with no window to add collateral.

### Finding Description
- `Pauseable.shutdown()` sets both `_everythingStopped` and `_paused` (`contracts/utils/Pauseable.sol:113-117`). [1](#0-0) 
- `Pool.liquidate()` is gated only by `whenNotShutdown` (`contracts/Pool.sol:537-549`) — confirmed by test `Pool.test.ts:353` ("should not revert if paused") and `docs/emergency-flags.md:63-65`, which state liquidation is disabled only when `everythingStopped()`. [2](#0-1) [3](#0-2) 
- `DebtToken.repay()`/`repayAll()` are likewise disabled only on `everythingStopped()` (`docs/emergency-flags.md:71-77`; test `DebtToken.test.ts:538-558` confirms `repayAll` works while paused and reverts only on shutdown).
- `DepositToken.deposit()` is disabled on `paused()` (`docs/emergency-flags.md:79-81`), and `withdraw()` on `everythingStopped()` (`docs/emergency-flags.md:83-85`). So during shutdown a borrower cannot repay, deposit collateral, or withdraw — they can only watch market movements push their `debtPositionOf` into unhealthy territory.
- `open()` clears only `_everythingStopped` (`contracts/utils/Pauseable.sol:97-100`). [4](#0-3)  This atomically re-enables `liquidate`, `repay`, and `repayAll` in the same transaction — the exact "repayment and liquidation resumed simultaneously" condition. Since `_paused` remains `true`, deposits stay disabled until a second governor call to `unpause()`, so even the client's suggested mitigation ("deposit more collateral") is unavailable in the window right after `open()`.
- There is no timestamp check, cooldown, or per-account grace window anywhere in `liquidate()` — the only guards are `amountToRepay_ != 0`, `account_ != msg.sender`, `!_isHealthy`, `maxLiquidable`, and `debtFloorInUsd` (`contracts/Pool.sol:553-579`).

### Impact Explanation
Direct, unfair loss of user funds. Borrowers whose positions became unhealthy during a shutdown — through market moves they had no ability to counteract — can be liquidated in the same block the pool reopens, paying the liquidation fee to MEV/liquidation bots, unless they can win a gas-bidding race to `repay`/`repayAll` in that same block. Because `deposit()` remains paused after `open()`, topping up collateral is not even an option until `unpause()` executes.

### Likelihood Explanation
Triggering requires a governor/guardian `shutdown()` followed by `open()` — a privileged but routine emergency action, not a malicious one. During any shutdown that coincides with adverse price movement of collateral or debt assets, at least some positions will cross the health threshold, and public liquidation bots monitor `debtPositionOf` continuously. The `open()` transaction itself is a public, predictable on-chain event that bots can back-run atomically, making exploitation near-certain whenever the precondition holds.

### Recommendation
- After `open()` (and `unpause()`), enforce a grace window before `liquidate()` becomes callable — e.g., record `liquidationUnlockedAt = block.timestamp + GRACE_PERIOD` in `open()`/`unpause()` and revert in `liquidate()` until it elapses.
- Alternatively, unfreeze `DepositToken.deposit()` before or simultaneously with re-enabling liquidation, and emit the grace deadline so front-ends can warn users.
- Order emergency resume operations so borrowers get at least one confirmed block — ideally a wall-clock window — to `repay`, `repayAll`, or `deposit` before liquidation resumes.

### Proof of Concept
Hardhat, extending the existing `liquidate` suite in `test/Pool.test.ts`:

```ts
it('liquidates a position that went unhealthy during shutdown, with no grace period', async function () {
  // alice has a healthy position (setup from existing beforeEach)

  // 1. Governor shuts everything down
  await pool.shutdown();

  // 2. Borrower is fully frozen: repay, deposit, withdraw, liquidate all revert
  await expect(msEthDebtToken.connect(alice).repayAll(alice.address))
    .revertedWithCustomError(msEthDebtToken, 'IsShutdown');
  await expect(msdMET.connect(alice).deposit(parseEther('1'), alice.address))
    .revertedWithCustomError(msdMET, 'IsPaused'); // shutdown also sets _paused

  // 3. Market moves against alice while frozen
  await masterOracle.updatePrice(met.address, toUSD('0.95'));
  expect((await pool.debtPositionOf(alice.address))._isHealthy).false;

  // 4. Governor reopens; in the SAME block a bot liquidates alice.
  //    Note: deposit() is still paused here — alice cannot add collateral.
  await pool.connect(governor).open();
  await expect(msdMET.connect(alice).deposit(parseEther('1'), alice.address))
    .revertedWithCustomError(msdMET, 'IsPaused'); // no way to improve health yet

  const tx = pool.connect(liquidator)
    .liquidate(msEth.address, alice.address, userMintAmount, msdMET.address);
  await expect(tx).emit(pool, 'PositionLiquidated'); // succeeds immediately
});
```

Key assertions: `repayAll` and `deposit` revert during shutdown; after `open()` liquidation succeeds while `deposit` still reverts, proving the absence of any grace window.

### Citations

**File:** contracts/utils/Pauseable.sol (L97-100)
```text
    function open() external virtual whenShutdown onlyGovernor {
        _everythingStopped = false;
        emit Open(_msgSender());
    }
```

**File:** contracts/utils/Pauseable.sol (L113-117)
```text
    function shutdown() external virtual whenNotShutdown canShutdown {
        _everythingStopped = true;
        _paused = true;
        emit Shutdown(_msgSender());
    }
```

**File:** contracts/Pool.sol (L537-549)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticToken_)
        onlyIfDepositTokenExists(depositToken_)
        returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee)
```

**File:** docs/emergency-flags.md (L63-77)
```markdown
## Pool.liquidate()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.issue()

Disabled if: `PoolRegistry.everythingStopped()` || `!SyntheticToken.isActive()` || `Pool.everythingStopped()` || `!DebtToken.isActive()`

## DebtToken.repay()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.repayAll()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`
```
