### Title
An attacker can fill a DepositToken's `maxTotalSupply` cap, permanently blocking users from topping up collateral and forcing their positions into liquidation - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
Analogous to the Symmio `balanceLimitPerUser` issue, Metronome enforces a hard per-collateral supply cap (`maxTotalSupply`) inside `DepositToken._mint`. Any unprivileged user can unilaterally push `totalSupply` to the cap via repeated `deposit()` calls (self-deposits, potentially flash-loan funded). Once the cap is reached, every subsequent `deposit()` on that collateral reverts with `SurpassMaxDepositSupply`, so users whose positions are deteriorating cannot add that collateral to restore health and become forcibly liquidatable via `Pool.liquidate`.

### Finding Description
`DepositToken.deposit()` mints msdTOKEN to `onBehalfOf_` via `_mint` [1](#0-0) . Inside `_mint`, the check `if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply()` reverts the whole transaction once the cap is hit — including the fee mint to `feeCollector` [2](#0-1) . There is no exemption for topping up an existing position, no grace margin, and no alternate "add collateral" entry point — `deposit()` is the only way to increase a position's collateral for that token.

A position is unhealthy when `debtOf(account) > issuableLimit` in `Pool.debtPositionOf` [3](#0-2) , after which anyone can seize collateral via `Pool.liquidate` [4](#0-3) .

Attack path (all unprivileged, public entry points):
1. Attacker observes `maxTotalSupply - totalSupply` headroom on a `DepositToken` (e.g. msdMET).
2. Attacker calls `deposit(headroom, attacker)` — possibly flash-loan funded — filling the cap. No modifier blocks this: `deposit` is only `whenNotPaused nonReentrant onlyIfDepositTokenExists`.
3. A victim holding that collateral type whose health factor decays (price move or interest accrual via `DebtToken.accrueInterest`) calls `deposit(x, victim)` → reverts `SurpassMaxDepositSupply`.
4. Attacker (or any liquidator) calls `Pool.liquidate(synth, victim, amountToRepay, msdTOKEN)` and seizes the victim's collateral plus the liquidator incentive.

### Impact Explanation
Users are denied the ability to add the capped collateral to defend their positions and suffer forced liquidation losses (seized collateral + incentive). If an attacker fills the caps of all listed deposit tokens, the protocol offers no in-protocol way to add collateral at all — the only remaining defense is acquiring the synthetic debt token externally and repaying, which the victim may not be able to do (the synthetic may be illiquid or the victim's debt token may itself be unobtainable). This is a loss-of-funds vector for affected users, matching the reported bug class.

### Likelihood Explanation
- Requires no privileged role: `deposit` is a public function and `maxTotalSupply` is finite and governor-set per collateral.
- Cost is bounded by remaining cap headroom; if a collateral is already near its cap organically, the attack is cheap and can be done atomically with a flash loan (deposited funds can be withdrawn again afterward via `withdraw`, subject to the attacker's own position health).
- Mitigation exists: victims can still deposit *other* listed collateral types (unless all are capped) or repay debt — so the forced liquidation is conditional on the victim's available alternatives, keeping this at medium severity.

### Recommendation
- Do not apply `maxTotalSupply` to top-ups of existing positions, or reserve headroom: e.g. revert only when `totalSupply > maxTotalSupply && balanceOf[account_] == 0`-style caps, or allow deposits that keep `debtPositionOf` healthy-check improvement.
- Alternatively, add a separate "collateral top-up" path exempt from the supply cap (collateral still accrues to solvency regardless of the cap accounting), mirroring the report's suggested margin approach: e.g. cap ordinary deposits at 90% of `maxTotalSupply` and reserve the remaining 10% for health-restoring deposits.

### Proof of Concept
Hardhat fork test sketch (against deployed Pool/DepositToken on e.g. Base):

```ts
// fork Base mainnet; impersonate attacker & victim EOA
const msdMET = await ethers.getContractAt('DepositToken', MSD_MET);
const pool = await ethers.getContractAt('Pool', POOL);
const met = await ethers.getContractAt('IERC20', await msdMET.underlying());

// 1) Victim has an open leveraged position that becomes unhealthy
//    (drop oracle price or accrue interest until debtPositionOf.isHealthy == false)
await setOraclePrice(/* lower MET price */);
expect((await pool.debtPositionOf(victim))._isHealthy).to.be.false;

// 2) Attacker fills the deposit cap
const headroom = (await msdMET.maxTotalSupply()).sub(await msdMET.totalSupply());
await deal(met, attacker, headroom); // or Balancer/Aave flash loan
await met.connect(attacker).approve(msdMET.address, headroom);
await msdMET.connect(attacker).deposit(headroom, attacker.address);
expect(await msdMET.totalSupply()).to.eq(await msdMET.maxTotalSupply());

// 3) Victim's top-up reverts
await met.connect(victim).approve(msdMET.address, topUpAmount);
await expect(
  msdMET.connect(victim).deposit(topUpAmount, victim.address)
).to.be.revertedWithCustomError(msdMET, 'SurpassMaxDepositSupply');

// 4) Attacker liquidates the victim
await pool.connect(attacker).liquidate(msEth.address, victim.address, amountToRepay, msdMET.address);
```

Key assertions: step 3 reverts for any `topUpAmount > 0` while the cap is saturated, and step 4 succeeds, seizing victim collateral — demonstrating the cap enables forced liquidation that the victim cannot defend against for that collateral.

### Citations

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

**File:** contracts/DepositToken.sol (L469-489)
```text
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();

        uint256 _balanceBefore = balanceOf[account_];
        unchecked {
            balanceOf[account_] = _balanceBefore + amount_;
        }

        emit Transfer(address(0), account_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/Pool.sol (L248-266)
```text
    function debtPositionOf(
        address account_
    )
        public
        view
        override
        returns (
            bool _isHealthy,
            uint256 _depositInUsd,
            uint256 _debtInUsd,
            uint256 _issuableLimitInUsd,
            uint256 _issuableInUsd
        )
    {
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
    }
```

**File:** contracts/Pool.sol (L537-569)
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
    {
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }

        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```
