### Title
Dust deposits on behalf of a victim fill `MAX_TOKENS_PER_USER`, permanently blocking new collateral deposits and trapping indebted positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DepositToken.deposit(amount_, onBehalfOf_)` lets any account mint deposit tokens to an arbitrary `onBehalfOf_` address. When the recipient's balance goes from 0 to >0, `_mint` calls `pool.addToDepositTokensOfAccount(account_)`, which reverts once `debtTokensOfAccount + depositTokensOfAccount >= MAX_TOKENS_PER_USER (30)`. An attacker can dust-fund up to 30 distinct `DepositToken`s into a victim's account, after which (a) the victim's own `deposit` into any *new* collateral type reverts, (b) any `transfer`/`transferFrom` of a new `DepositToken` to the victim reverts, and (c) minting a new synthetic debt type reverts. The victim cannot remove entries because `removeFromDepositTokensOfAccount` is only callable by the token when a balance reaches zero, and transferring the dust out is gated by `_revertIfLocked` / `unlockedBalanceOf`, which returns 0 once the victim's `issuableInUsd` is exhausted by debt.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` enforces a shared cap of 30 across `debtTokensOfAccount` and `depositTokensOfAccount` and reverts with `UserReachedMaxTokens` [1](#0-0) 
- `addToDepositTokensOfAccount` is callable only from a registered `DepositToken`, but `DepositToken.deposit` is a public entry point that mints to any `onBehalfOf_` [2](#0-1) 
- `_mint` (and `_transfer`) add the token to the recipient's per-account list whenever the prior balance was 0 [3](#0-2) 
- Removal only happens inside `_burn`/`_transfer` when the sender's balance hits 0; there is no public "remove" path for the victim [4](#0-3) 
- Sending the dust back out is blocked for an indebted account: `transfer`/`withdraw` run `_revertIfLocked`, and `unlockedBalanceOf` returns 0 when `_issuableInUsd == 0` (debt at the collateral-factor limit) [5](#0-4) 

### Impact Explanation
Availability impact matching the InnoDB-hang bug class. A victim whose position drifts toward the liquidation threshold normally rescues it by depositing additional collateral — including a *different* collateral type when the original asset is unavailable or crashing. After the attacker's dust deposits fill the 30-slot list, every deposit of a new collateral type reverts inside `pool.addToDepositTokensOfAccount`, and the victim cannot evict the dust tokens because their whole balance is locked (`unlockedBalanceOf == 0`). The position is then force-liquidated. This is a reproducible denial of a core protocol function (deposit/collateral top-up) and leads to loss of the victim's collateral via liquidation that the user was powerless to prevent.

### Likelihood Explanation
Fully reachable by an unprivileged attacker: only public `DepositToken.deposit(smallAmount, victim)` calls with attacker-owned underlying are needed. Cost is bounded (≤30 tiny deposits plus gas). The condition that maximizes impact — victim near their collateral-factor limit so the dust is locked — is a normal market state, and the attacker can time the dusting when the victim's position is already stressed. The only partial mitigation is that deposits into collateral types the victim already holds still work (balance > 0 skips `add`), and a healthy victim can transfer dust out before taking on debt.

### Recommendation
- Do not let unsolicited `deposit`/`transfer` to a zero-balance account consume a slot without consent, or allow `addToDepositTokensOfAccount` to silently no-op/emit instead of reverting when the cap is hit (deposit succeeds; the token is simply not tracked for health checks — combined with treating untracked balances as non-collateral).
- Alternatively, let any account prune its own list via a public `removeFromDepositTokensOfAccount` gated on `balanceOf(account) == 0`-equivalent conditions, or exempt dust-below-threshold balances from locking in `unlockedBalanceOf`.
- Consider splitting the cap check so liquidation-critical paths (seize, repay) are never blocked by `UserReachedMaxTokens` on the *receiving* side.

### Proof of Concept
Hardhat sketch (against the repo's existing fixtures, similar to `test/Pool.test.ts`'s "should revert when reach max tokens" but driven through real `DepositToken.deposit` calls):

```ts
// 1. Victim deposits collateralA and mints msUSD up to the CF limit.
await depositTokenA.connect(victim).deposit(amount, victim.address);
await debtToken.connect(victim).issue(maxIssuable, victim.address); // issuableInUsd == 0 now

// 2. Attacker dust-deposits every other registered deposit token to victim.
for (const dt of otherDepositTokens /* up to 30 - existing */) {
  await underlying(dt).connect(attacker).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST, victim.address); // adds to victim's list
}
// victim's combined list now == MAX_TOKENS_PER_USER

// 3. Victim tries to rescue position with collateralB -> reverts
await expect(
  depositTokenB.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4. Victim cannot evict dust: all balances locked because issuableInUsd == 0
await expect(
  otherDepositTokens[0].connect(victim).transfer(attacker.address, DUST)
).to.be.revertedWithCustomError(otherDepositTokens[0], 'NotEnoughFreeBalance');

// 5. Oracle price moves against victim -> liquidate succeeds, rescue was impossible
await masterOracle.updatePrice(collateralA, lowerPrice);
await pool.connect(liquidator).liquidate(synthetic, victim.address, debtToCover, depositTokenA.address);
```

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

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```
