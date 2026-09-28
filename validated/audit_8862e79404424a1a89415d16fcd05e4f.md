### Title
`RewardsDistributor` accrues `balance * INITIAL_INDEX` phantom rewards when a tracked token's index is still at `INITIAL_INDEX` — ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
Analogous to the IoTeX bug (uninitialized `epochNum` → crash), `RewardsDistributor._calculateTokenDelta` leaves `accountIndex` effectively unassigned in the boundary case where `tokenStates[token].index == INITIAL_INDEX`. The guard `if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX)` only resets the account index when the global index has *already advanced past* `INITIAL_INDEX`. When it is exactly equal (right after a token is registered via `updateTokenSpeed`/`syncTokenSpeed`, before any timestamp elapses), `_accountIndex` stays `0`, producing `_deltaIndex = 1e18 - 0 = 1e18` and `_tokensDelta = balance * 1e18` phantom reward accrual for any account holding the token. [1](#0-0) 

### Finding Description
When a reward token is first registered, `_updateTokenSpeed` sets `tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` and pushes it into `tokens[]`. [2](#0-1) 

Any subsequent accrual path — `claimRewards`, or the permissionless `updateBeforeMintOrBurn`/`updateBeforeTransfer` (invoked automatically by every `DepositToken`/`DebtToken` mint, burn, transfer, deposit, withdraw, seize, and liquidation through `updateRewardsBeforeMintOrBurn`/`updateRewardsBeforeTransfer`) — calls `_updateTokensAccruedOf` because `tokenStates[token].index > 0`. [3](#0-2) [4](#0-3) 

In the same block/timestamp as registration, `_calculateTokenIndex` returns `(0, 0)` (`_deltaTimestamps == 0`), so the stored state stays `{INITIAL_INDEX, T}`. [5](#0-4)  Then in `_calculateTokenDelta`, `_accountIndex == 0` but `_tokenIndex > INITIAL_INDEX` is false (it is equal), so `_accountIndex` remains `0` and `_tokensDelta = token_.balanceOf(account_).wadMul(1e18)`. `_updateTokensAccruedOf` permanently writes this huge delta into `tokensAccruedOf[account]`. [6](#0-5) 

`claimRewards` then pays `tokensAccruedOf[account]` capped only by the distributor's `rewardToken` balance, silently zeroing it via `_transferRewardIfEnoughTokens`. [7](#0-6) [8](#0-7) 

No access control stops this: `updateBeforeMintOrBurn` is callable by anyone ("This function also may be called by anyone"), and `claimRewards` has no `onlyIfDistributorExists`/sender checks. [9](#0-8) 

### Impact Explanation
Direct theft of unclaimed yield / reward tokens. A user (or attacker depositing dust collateral beforehand) accrues `balance × 1e18` reward tokens and drains the distributor's entire `rewardToken` balance, up to the contract's holdings. If the balance is insufficient at claim time, the inflated `tokensAccruedOf` persists and drains any rewards funded later, permanently stealing rewards owed to all other users.

### Likelihood Explanation
The trigger window is the same timestamp as token registration. An attacker can hold a `DepositToken`/`DebtToken` balance in advance and either (a) land `updateBeforeMintOrBurn(token, attacker)` in the same block as the governor's `updateTokenSpeed`/`updateTokenSpeeds` or keeper's `syncTokenSpeed` (same `block.timestamp`, e.g. via mempool monitoring/Flashbots-style inclusion), or (b) simply be the recipient of any transfer/mint/burn in that block. Additionally, if a speed is set and then set back to `0` within the same timestamp (index stays at `INITIAL_INDEX`), the boundary condition persists indefinitely — every subsequent holder accrues `balance × 1e18` until the index finally advances. Requires a `RewardsDistributor` registered on the pool with a nonzero speed scheduled, which is the deployed configuration on chains where rewards are active.

### Recommendation
Change the boundary check to `if (_accountIndex == 0)` unconditionally (or `<= INITIAL_INDEX`), i.e. initialize the account index to the current `tokenState.index` whenever `accountIndexOf` is unset — including when the index equals `INITIAL_INDEX`:

```solidity
// contracts/RewardsDistributor.sol _calculateTokenDelta
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = _tokenIndex; // or INITIAL_INDEX
}
```

Equivalent fix: assign `_accountIndex = _tokenIndex` (not `INITIAL_INDEX`) so a never-synced account starts at the current index rather than the stale initial one, mirroring the reference fix that assigned `epochNum`.

### Proof of Concept
Hardhat/Foundry fork sketch (see `test/RewardsDistributor.ts` patterns for distributor/pool wiring):

```solidity
// 1. Attacker deposits collateral -> holds DepositToken balance B (nonzero).
depositToken.deposit(amount, attacker);

// 2. Governor (or tokenSpeedKeeper via syncTokenSpeed) registers reward:
//    tokenStates[depositToken] = {index: 1e18, timestamp: now}
vm.prank(governor);
rewardsDistributor.updateTokenSpeed(IERC20(depositToken), speed);

// 3. Same block (timestamp unchanged): permissionless accrual.
//    _calculateTokenIndex -> (0,0); state stays at INITIAL_INDEX.
//    _calculateTokenDelta: accountIndex=0, tokenIndex=1e18 (not >), deltaIndex=1e18.
rewardsDistributor.updateBeforeMintOrBurn(IERC20(depositToken), attacker);
assertEq(rewardsDistributor.tokensAccruedOf(attacker), B * 1e18); // phantom accrual

// 4. Drain the distributor's rewardToken balance.
rewardsDistributor.claimRewards(attacker);
assertEq(rewardToken.balanceOf(attacker), distributorRewardBalance);
```

### Citations

**File:** contracts/RewardsDistributor.sol (L134-145)
```text
    function claimRewards(address account_) external override {
        claimRewards(account_, tokens);
    }

    /**
     * @notice Claim tokens accrued by account in the specified tokens
     */
    function claimRewards(address account_, IERC20[] memory tokens_) public override {
        address[] memory _accounts = new address[](1);
        _accounts[0] = account_;
        claimRewards(_accounts, tokens_);
    }
```

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

**File:** contracts/RewardsDistributor.sol (L175-191)
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
```

**File:** contracts/RewardsDistributor.sol (L201-211)
```text
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
```

**File:** contracts/RewardsDistributor.sol (L222-230)
```text
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L248-255)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
```

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```

**File:** contracts/DepositToken.sol (L124-143)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
    }

    /**
     * @notice Update reward contracts' states
     * @dev Should be called before balance changes (i.e. transfer)
     */
    modifier updateRewardsBeforeTransfer(address sender_, address recipient_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeTransfer(this, sender_, recipient_);
        }
        _;
```
