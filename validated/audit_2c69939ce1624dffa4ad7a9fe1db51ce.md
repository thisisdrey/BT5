### Title
Dust-transfer griefing fills victim's `MAX_TOKENS_PER_USER` slots, blocking all deposits of new collateral - (File: contracts/Pool.sol)

### Summary
`DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance of that token was zero [1](#0-0) . `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` across the sum of a user's deposit tokens and debt tokens, reverting with `UserReachedMaxTokens` once the cap is reached [2](#0-1) [3](#0-2) . Because deposit tokens are freely transferable unlocked balances [4](#0-3) , any unprivileged attacker can push dust amounts of up to 30 distinct deposit tokens into a victim's account, filling their list. Afterwards, every `DepositToken.deposit(..., onBehalfOf_ = victim)` for a collateral type the victim does not already hold reverts inside `_mint` when it tries to register the new token [5](#0-4) . The same applies to deposits routed through `NativeTokenGateway`, `VesperGateway`, `SmartFarmingManager.leverage`, and `Operator.execute`.

### Finding Description
- The recipient's token-list capacity is consumed by an action the recipient never consented to: `transfer`/`transferFrom` validate only the sender's unlocked balance (`_revertIfLocked`) and never validate that the recipient can accept a new token entry [6](#0-5) .
- Attack path: attacker deposits a minimal amount of collateral into each pool deposit token (no debt, so the full balance is unlocked), then calls `depositToken.transfer(victim, 1)` for each distinct deposit token. Each call adds an entry to `depositTokensOfAccount[victim]` via `_transfer` → `addToDepositTokensOfAccount` [7](#0-6) .
- Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) >= 30`, `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert, so `deposit` (which calls `_mint` → `addToDepositTokensOfAccount`) and `issue`/`mint` of new debt positions to the victim revert [3](#0-2) [8](#0-7) .
- This is improper-input-validation-driven DoS (the CVE class): a state-mutating input (`to_`) is accepted without checking its effect on per-account limits, and no privileged actor, oracle manipulation, or bad debt is required. The invariant broken is liveness — a user can be prevented from onboarding collateral or opening positions.

### Impact Explanation
The victim is denied service: they cannot deposit any collateral type not already in their list, cannot receive new deposit tokens, cannot be leveraged into via `SmartFarmingManager`, and cannot be deposited to via gateways or `Operator.execute` multicalls. If the victim has an unhealthy position that requires new collateral to become healthy, griefing their token list also blocks rescue deposits of new collateral types, indirectly enabling liquidations. Victims can partially self-recover by zeroing out a dust balance (which triggers `removeFromDepositTokensOfAccount`), but the attacker can re-fill slots repeatedly at dust cost, making the DoS persistent and economically cheap relative to the harm.

### Likelihood Explanation
Requires no privileges, no flash loan even, only the attacker's own collateral — and the collateral is recoverable afterwards by withdrawing the attacker's remaining balance, so cost is essentially gas plus deposit fees on dust. The constraint is that the pool must list enough deposit tokens (or the victim must already hold some debt/deposit tokens) to reach the cap of 30; on deployments with many collaterals this is reachable. Pools with few deposit tokens may not reach the cap, so feasibility depends on the deployed token count.

### Recommendation
Do not charge the recipient's `MAX_TOKENS_PER_USER` budget for unsolicited dust. Options: (a) exempt transfers from list-registration when `amount_` is below a minimum economic threshold, (b) in `addToDepositTokensOfAccount`, treat a revert-on-cap as a no-op skip rather than reverting the whole transfer for tokens the recipient would never meaningfully hold, or (c) allow the recipient to purge entries themselves via a public `removeFromDepositTokensOfAccount` callable when `balanceOf == 0` (currently restricted to deposit-token callers). At minimum, make `deposit` succeed without registering tokens beyond the cap only for transfers, keeping the cap enforced for genuine deposits.

### Proof of Concept
Hardhat fork sketch:

```ts
// Assume pool has >= 30 deposit tokens registered: dt0..dt29
const victim = alice.address
for (let i = 0; i < 30; i++) {
  const dt = depositTokens[i]
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.connect(attacker).approve(dt.address, ethers.constants.MaxUint256)
  await dt.connect(attacker).deposit(ATTACKER_DUST_AMOUNT, attacker.address) // no debt -> fully unlocked
  await dt.connect(attacker).transfer(victim, 1) // adds dt_i to depositTokensOfAccount[victim]
}
expect(await pool.getDepositTokensOfAccount(victim)).to.have.lengthOf(30)

// Victim tries to deposit a 31st collateral type (or any type they don't hold)
const newDt = depositTokens[30]
const underlying31 = await ethers.getContractAt('IERC20', await newDt.underlying())
await underlying31.connect(alice).approve(newDt.address, ethers.constants.MaxUint256)
await expect(newDt.connect(alice).deposit(amount, alice.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// Transfers of any new token to victim also revert
await expect(depositTokens[29].connect(attacker).transfer(victim, 1)).to.not.be.reverted // already-held, ok
await expect(newDt.connect(attacker).transfer(victim, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Note: I could not fully verify the number of registered deposit/debt tokens in the live deployments (deployment JSONs confirm the `Pool`/`DepositToken` ABIs but not the token count), so whether 30 slots are reachable is deployment-dependent; the revert path itself (`transfer` → `_transfer` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`) is confirmed by `test/Pool.test.ts` coverage of the cap.

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

**File:** contracts/DepositToken.sol (L348-376)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

        return true;
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

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```
