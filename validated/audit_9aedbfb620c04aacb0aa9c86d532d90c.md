### Title
Flash-loaned deposit lets an attacker steal accrued rewards from `RewardsDistributor` in a single transaction - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` distributes a per-second reward stream pro-rata over a reward token's `totalSupply`, and credits each account using its **spot `balanceOf` at claim time** (Synthetix-style index accounting). An attacker can flash-loan the underlying, deposit via `DepositToken.deposit` to mint a dominant share of the reward token's supply, call `claimRewards` in the same transaction to collect essentially all rewards accrued since the last index update, then immediately `withdraw` and repay the loan. Rewards that economically belong to long-term depositors are drained to the attacker's account because the index delta is multiplied by the attacker's just-minted balance. [1](#0-0) 

### Finding Description
The reward math lives in two functions:

- `_calculateTokenIndex` accrues `_tokensAccrued = deltaTimestamps * speed` and divides by `token_.totalSupply()` at the moment of the call: [2](#0-1) 
- `_calculateTokenDelta` multiplies the index delta by the account's **current** balance: `_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex)`: [3](#0-2) 

`DepositToken._mint` calls `updateBeforeMintOrBurn` *before* increasing the balance, so the attacker's `accountIndexOf` is pinned to the pre-mint index — correct as far as it goes. The flaw is that the *global* index delta accumulated over the entire `deltaTimestamps` period is then divided by the post-mint inflated `totalSupply`, and the attacker's share is computed on his full post-mint balance. If the attacker's minted amount `A` dominates `totalSupply` (`A >> S`), his delta is `A * elapsed*speed/(S+A) ≈ elapsed*speed` — i.e., he harvests ~100% of all rewards accrued over `elapsed` seconds while having held the position for zero time. [4](#0-3) 

Attack trace (all in one transaction, no privileges needed):

1. Flash-loan the deposit token's `underlying` (e.g. via Balancer/Aave/Uniswap or `SmartFarmingManager`-style external liquidity).
2. `DepositToken.deposit(amount, attacker)` — mints `msdTOKEN` to attacker; `updateRewardsBeforeMintOrBurn` sets `accountIndexOf[msdTOKEN][attacker]` to the stale global index. [5](#0-4) 
3. `RewardsDistributor.claimRewards(attacker, [msdTOKEN])` — `_updateTokenIndex` computes the index delta for the whole elapsed period against the inflated supply, `_updateTokensAccruedOf` credits `attackerBalance * deltaIndex`, and `_transferRewardIfEnoughTokens` pays out `rewardToken` up to the contract's balance. [6](#0-5) 
4. `DepositToken.withdraw(amount, attacker)` — `unlockedBalanceOf` returns the full balance when `debtInUsd == 0`, so `_revertIfLocked` passes; the underlying is pulled from `Treasury` back to the attacker. [7](#0-6) 
5. Repay the flash loan; keep the claimed `rewardToken`.

This is the same bug class as the referenced GPToke report (rewards computed on spot balance, claimable in the same transaction as the stake). Here the index-based variant does not prevent it: the index only ensures the attacker can't claim rewards accrued *before* the last global index update — but everything accrued since that update is capturable by dominating the supply denominator. `claimRewards`, `updateBeforeMintOrBurn`, and `updateBeforeTransfer` are all permissionless public entry points, so the whole flow is reachable by an EOA/attacker contract. [8](#0-7) 

Nothing stops it on the deployed configuration: `deposit`/`withdraw`/`claimRewards` carry `nonReentrant`/`whenNotPaused` only, there is no minimum-holding-period or per-block stake/claim separation, and `_revertIfLocked` is satisfied because the attacker opens no debt position. [9](#0-8) 

### Impact Explanation
Direct theft of unclaimed yield. Every reward token streamed to a `DepositToken` (or `DebtToken`, via `DebtToken.issue`/`flashIssue`/`principalOf`-based balances — the same `_calculateTokenDelta` math applies) between two index updates can be siphoned by whoever can transiently dominate `totalSupply`. Repeatable each time a meaningful amount accrues (bounded by `RewardsDistributor`'s `rewardToken` balance and `maxTotalSupply` caps on `DepositToken`), with the only cost being flash-loan fees and deposit/withdraw fees.

### Likelihood Explanation
Requires a reward token with `tokenSpeed > 0`, an index that has gone unstale for a while (elapsed accrual worth more than the attack cost), and flash-loanable underlying liquidity sufficient to dominate supply — all realistic on mainnet deployments where index updates only happen when users interact. Profit scales with accrual staleness, so the attack is most lucrative precisely when rewards have piled up.

### Recommendation
Apply the report's recommendation adapted to this codebase: prevent minting reward-eligible balance and claiming accrued rewards within the same block/transaction — e.g., record a per-account `lastMintTimestamp`/`lastMintBlock` per reward token in `RewardsDistributor.updateBeforeMintOrBurn` and either (a) revert or (b) zero out the delta in `claimRewards` when `balanceOf` was increased in the same block. Alternatively, snapshot eligibility (reward only balances held from before the current accrual window) or vest claimed rewards over time.

### Proof of Concept
Hardhat sketch (assuming a deployed-style setup: `pool`, `msdETH` DepositToken over WETH, `rewardDistributor` holding `rewardToken`, `tokenSpeed[msdETH] > 0`, index last updated `T` seconds ago, honest `totalSupply = S`):

```ts
// Attacker contract holds a flash-loan callback for WETH
it('steals accrued rewards via flash deposit', async () => {
  const S = await msdETH.totalSupply();               // honest supply
  const elapsed = await timeSinceLastIndexUpdate();   // seconds since tokenStates.timestamp
  const speed = await rewardDistributor.tokenSpeeds(msdETH.address);

  // Flash-loan A >> S of WETH, inside the callback:
  await weth.approve(msdETH.address, A);
  await msdETH.deposit(A, attacker);                  // mints ~A msdETH, pins accountIndex

  const balBefore = await rewardToken.balanceOf(attacker);
  await rewardDistributor.claimRewards(attacker.address, [msdETH.address]);
  const stolen = (await rewardToken.balanceOf(attacker)).sub(balBefore);

  // stolen ≈ elapsed * speed * A/(S+A) ≈ elapsed * speed
  expect(stolen).to.be.closeTo(speed.mul(elapsed), speed.mul(elapsed).div(50));

  await msdETH.withdraw(await msdETH.balanceOf(attacker), attacker); // unlocked: no debt
  // repay flash loan, keep `stolen`
});
```

Uncertainties: profit is bounded by `maxTotalSupply` headroom and deposit/withdraw fees, and by how stale the token index is; if the index is updated every block by organic activity the per-tx capture shrinks to one block's accrual. The `DebtToken` path (rewarding borrowers) is susceptible to the same math but requires opening a debt position with collateral, making the deposit-token route the cleanest analog.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-168)
```text
    function claimRewards(address[] memory accounts_, IERC20[] memory tokens_) public override nonReentrant {
        uint256 _accountsLength = accounts_.length;
        uint256 _tokensLength = tokens_.length;
        for (uint256 i; i < _tokensLength; ++i) {
            IERC20 _token = tokens_[i];

            if (tokenStates[_token].index > 0) {
                _updateTokenIndex(_token);
                for (uint256 j; j < _accountsLength; j++) {
                    _updateTokensAccruedOf(_token, accounts_[j]);
                }
            }
        }

        for (uint256 j; j < _accountsLength; j++) {
            address _account = accounts_[j];
            _transferRewardIfEnoughTokens(_account, tokensAccruedOf[_account]);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L175-192)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }

    /**
     * @notice Update indexes on pre-transfer
     * @dev Called by DepositToken and DebtToken contracts
     */
    function updateBeforeTransfer(IERC20 token_, address from_, address to_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, from_);
            _updateTokensAccruedOf(token_, to_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L197-212)
```text
    function _calculateTokenIndex(
        TokenState memory _supplyState,
        IERC20 token_
    ) private view returns (uint224 _newIndex, uint32 _newTimestamp) {
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
        } else if (_deltaTimestamps > 0 && _supplyState.index > 0) {
            _newTimestamp = block.timestamp.toUint32();
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L217-231)
```text
    function _calculateTokenDelta(
        TokenState memory _tokenState,
        IERC20 token_,
        address account_
    ) private view returns (uint256 _tokenIndex, uint256 _tokensDelta) {
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
    }
```

**File:** contracts/RewardsDistributor.sol (L248-256)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
    }
```

**File:** contracts/DepositToken.sol (L124-131)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
    }
```

**File:** contracts/DepositToken.sol (L180-183)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
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

**File:** contracts/DepositToken.sol (L383-412)
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

    /**
     * @notice Burn msdTOKEN and withdraw collateral
     * @param amount_ The amount of collateral to withdraw
     * @param to_ The account that will receive withdrawn collateral
     * @return _withdrawn The amount withdrawn after fees
     */
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
    }
```
