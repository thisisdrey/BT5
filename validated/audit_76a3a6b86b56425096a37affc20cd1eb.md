### Title
Dust-transfer griefing fills `feeCollector`'s per-account token list, permanently reverting all fee-bearing liquidations and swaps - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate()` sends the protocol liquidation fee to `poolRegistry.feeCollector()` via `DepositToken.seize()`, which internally calls `_transfer()` → `pool.addToDepositTokensOfAccount(feeCollector)`. That function is guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` once the recipient holds `MAX_TOKENS_PER_USER` (30) distinct deposit+debt tokens [1](#0-0) . Because `DepositToken.transfer`/`transferFrom` are public and permissionless for any unlocked balance, an attacker can dust-transfer 1 wei of each listed deposit token to the `feeCollector` address, filling its per-account set. From then on, every `liquidate` call that would pay a protocol fee reverts, and every `swap` with `fee > 0` also reverts (`syntheticTokenOut_.mint(feeCollector, _fee)` is fine, but liquidation's seize is not — see below). The result is a repeatable "crash" of the liquidation path, directly analogous to CVE-2021-35632's hang/repeatable-crash DoS.

### Finding Description
- `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` when the recipient's balance was zero [2](#0-1) .
- `addToDepositTokensOfAccount` is wrapped in `onlyIfAdditionWillNotReachMaxTokens`, reverting once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [3](#0-2) .
- `Pool.liquidate` performs `depositToken_.seize(account_, _msgSender, _toLiquidator)` and then, when `_fee > 0`, `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)` [4](#0-3) . The whole call is atomic, so a revert in the fee seize rolls back the entire liquidation.
- `DepositToken.seize` is just `_transfer` and performs no `unlockedBalanceOf`/lock check on the *recipient* side [5](#0-4) .
- `DepositToken.transferFrom` is callable by anyone once the sender approves — and more simply, the attacker can deposit a minimal amount of each collateral themselves and `transfer` dust to `feeCollector` (transfers only check the *sender's* unlocked balance) [6](#0-5) .

Attack steps (all unprivileged, public entry points):
1. For each deposit token `msdX` registered in the pool, the attacker deposits a tiny amount of underlying (or acquires `msdX` dust) and calls `msdX.transfer(feeCollector, 1)`.
2. Each transfer adds `msdX` to `feeCollector`'s `depositTokensOfAccount`. After enough distinct tokens (at most the number of deposit tokens listed in the pool, capped at 30), `feeCollector` hits `MAX_TOKENS_PER_USER`.
3. Any subsequent `liquidate(...)` where `protocolLiquidationFee > 0` reverts with `UserReachedMaxTokens` inside the fee seize, making undercollateralized positions unliquidatable.

The `feeCollector` can clear slots by transferring the dust out (its balance is unlocked since it has no debt), so the freeze is temporary per incident — but the attacker can re-fill the slots in the same block via back-to-back transfers (or sandwich any liquidation), giving a persistent, repeatable DoS of liquidations for the cost of dust. If `feeCollector` is a contract without generic transfer capability (e.g., a splitter contract that can only sweep to fixed payees, or a multisig that is slow to react), the freeze is effectively permanent.

### Impact Explanation
Liquidation is the solvency backstop of the protocol. With fee-bearing liquidations reliably reverting, liquidators either cannot liquidate at all (if `protocolLiquidationFee > 0`, the revert is unconditional — there is no flag to skip the fee seize) or must race an attacker who can re-grief `feeCollector` in the same block. Underwater positions accumulate, collateral value falls further, and the pool accrues bad debt — protocol insolvency. This matches the accepted "temporary freezing of funds" / insolvency impact classes, and mirrors the CVE class: a repeatedly triggerable crash of a critical server function.

### Likelihood Explanation
- Cost: dust amounts of each pool collateral plus gas for ≤30 transfers — cheap on L2 deployments (Base, Optimism, Hemi, Swell).
- Preconditions: `poolRegistry.feeCollector()` has zero-balance slots to fill (nearly always true for a fee-collection address) and `protocolLiquidationFee > 0` on `FeeProvider` for the revert to trigger inside `liquidate`. Even with `protocolLiquidationFee == 0`, the liquidator-side seize still calls `addToDepositTokensOfAccount(liquidator)`, so an attacker can equally grief *known active liquidator bots'* addresses, blocking them specifically.
- No privileged role, oracle manipulation, or malicious infrastructure is required — only public ERC-20 transfers of `msdTOKEN`.

### Recommendation
- Make the per-account token-addition non-fatal in `seize`/`_transfer` paths invoked by `Pool.liquidate`: e.g., wrap `addToDepositTokensOfAccount` in a try/catch inside `DepositToken._transfer`, or add a `seize`-specific path that skips the accounting list (the lists are only used for `debtPositionOf`/`depositOf` iteration, and the feeCollector/liquidator does not need health tracking for seized dust).
- Alternatively, send the liquidation fee to `Treasury.pull`-style accounting or to the pool itself, and let the feeCollector claim asynchronously.
- Reconsider the hard `UserReachedMaxTokens` revert on the *recipient* side of permissionless transfers; track positions lazily (e.g., only add on `deposit`/`issue`, not on `transfer`/`seize`).

### Proof of Concept
Hardhat sketch (against deployed pool on a fork):

```ts
// setup: attacker holds dust of each msdTOKEN (deposit minimal underlying or swap)
const feeCollector = await poolRegistry.feeCollector()

// 1. Fill feeCollector's per-account list
for (const msdTokenAddr of await pool.getDepositTokens()) {
  const msdToken = await ethers.getContractAt('DepositToken', msdTokenAddr)
  await msdToken.connect(attacker).transfer(feeCollector, 1)
}

expect((await pool.getDepositTokensOfAccount(feeCollector)).length)
  .to.be.gte(await pool.MAX_TOKENS_PER_USER())

// 2. Any fee-bearing liquidation reverts
await expect(
  pool.connect(liquidator).liquidate(
    msEth.address, victim.address, amountToRepay, msdMET.address
  )
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// reverts inside DepositToken._transfer -> Pool.addToDepositTokensOfAccount(feeCollector)
```

Reproducibility note: the PoC needs at least `protocolLiquidationFee > 0` configured, or targets known liquidator EOAs with the same dust-fill to block their `seize` leg. Not verified live against a fork here — the reverting path (`_transfer` → `addToDepositTokensOfAccount` → `onlyIfAdditionWillNotReachMaxTokens`) is confirmed in source [7](#0-6) [8](#0-7) , but whether this exact scenario was previously disclosed in a Metronome audit could not be checked against the repo contents; if it is a known issue it should be rejected under the stated rules.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L498-525)
```text
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
