### Title
Rewards emitted while deposit/debt token supply is zero are permanently skipped - (File: `contracts/RewardsDistributor.sol`)

### Summary
`RewardsDistributor` advances a reward token’s timestamp without increasing its index when the tracked `DepositToken` or `DebtToken` has zero total supply. Emissions for that elapsed interval are therefore never assigned to any account. An unprivileged user can force the interval to be discarded by calling the public `updateBeforeMintOrBurn()` while supply is zero; the timestamp advances and the skipped emissions cannot later be distributed.

### Finding Description
Reward emissions are represented by `tokenSpeeds[token_]`. Each update calculates elapsed time and derives an index increment as `tokensAccrued / token_.totalSupply()`. When `totalSupply()` is zero, `_ratio` is explicitly set to zero, but `_newTimestamp` is still set to `block.timestamp` and persisted by `_updateTokenIndex()`. [1](#0-0) [2](#0-1) 

`updateBeforeMintOrBurn()` is public and explicitly documented as callable by anyone to update stored indexes. It has no token-registration, caller, pause, or supply check beyond `tokenStates[token_].index > 0`. [3](#0-2) 

Consequently:

1. A deposit or debt token has nonzero `tokenSpeed`.
2. Its token supply is zero—for example, before first deposit/borrow or after all positions exit.
3. Time passes with `speed > 0`.
4. Anyone calls `updateBeforeMintOrBurn(token, account)`.
5. `_calculateTokenIndex()` computes `_tokensAccrued = elapsed * speed`, but uses `_ratio = 0` because supply is zero.
6. `_updateTokenIndex()` stores only the new timestamp.
7. The emissions for the elapsed period are permanently omitted from the reward index.

DepositToken calls this hook before minting, so the zero-supply period is necessarily discarded when the first deposit token is minted. [4](#0-3) 

### Impact Explanation
Rewards emitted during zero-supply intervals are permanently excluded from `tokenStates[token].index` and can never become claimable through `claimRewards()`. Claims only transfer amounts accumulated into `tokensAccruedOf`; there is no separate mechanism in `RewardsDistributor` that later credits skipped emissions. [5](#0-4) [6](#0-5) 

This permanently freezes or strands unclaimed protocol yield intended for holders of the relevant deposit or debt token. The loss equals `elapsedZeroSupplyTime * tokenSpeed`, subject to index precision.

### Likelihood Explanation
The condition can arise in normal operation:

- A rewarded deposit token may exist before its first deposit.
- All depositors may withdraw, reducing `DepositToken.totalSupply()` to zero.
- All borrowers may repay, reducing `DebtToken.totalSupply()` to zero.
- A first mint calls the reward hook before the balance and supply change, so the preceding zero-supply interval is discarded automatically. [4](#0-3) 
- Any user may additionally checkpoint the interval manually through `updateBeforeMintOrBurn()`. [7](#0-6) 

No attacker needs governor access, keeper access, oracle manipulation, or privileged protocol roles. Triggering the loss does not require ownership of the tracked token.

### Recommendation
Do not advance the reward timestamp without accounting for emissions while supply is zero. Suitable fixes include:

- Preserve `timestamp` when `totalSupply == 0`, allowing the next positive-supply update to distribute the entire elapsed emission amount.
- Maintain a pending accrual amount carried forward until supply becomes nonzero.
- If skipping emissions is intended, provide an authorized sweep/recovery mechanism and explicitly document that zero-supply emissions are protocol-recoverable rather than user rewards.

Carrying emissions forward is generally preferable if `tokenSpeed` represents rewards owed to future holders.

### Proof of Concept
The following Foundry-style test demonstrates the discarded interval:

```solidity
function test_zeroSupplyEmissionsArePermanentlySkipped() public {
    IERC20 depositToken = IERC20(address(msdToken));

    // Reward stream is active before the token has supply.
    vm.prank(governor);
    rewardsDistributor.updateTokenSpeed(depositToken, 1 ether);

    assertEq(depositToken.totalSupply(), 0);

    // Ten seconds of emissions accrue while no holder exists.
    skip(10);

    // Unprivileged caller checkpoints the zero-supply period.
    rewardsDistributor.updateBeforeMintOrBurn(depositToken, alice);

    (uint224 indexAfterZeroSupply, uint32 timestampAfterZeroSupply) =
        rewardsDistributor.tokenStates(depositToken);

    assertEq(indexAfterZeroSupply, rewardsDistributor.INITIAL_INDEX());
    assertEq(timestampAfterZeroSupply, uint32(block.timestamp));

    // Alice mints the entire initial supply after the skipped period.
    depositToPool(alice, depositToken, 100 ether);

    skip(10);

    uint256 claimable = rewardsDistributor.claimable(alice);

    // Alice receives only the positive-supply interval: 10 reward units.
    // The preceding 10 reward units are absent from the index forever.
    assertEq(claimable, 10 ether);
}
```

A lower-level unit test can replace `depositToPool()` by mocking `depositToken.totalSupply()` to first return `0` and then `100 ether`; the important state transition is that `_updateTokenIndex()` persists `block.timestamp` while adding zero to the index during the zero-supply interval.

### Citations

**File:** contracts/RewardsDistributor.sol (L170-179)
```text
    /**
     * @notice Update indexes on pre-mint and pre-burn
     * @dev Called by DepositToken and DebtToken contracts
     * This function also may be called by anyone to update stored indexes
     */
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
```

**File:** contracts/RewardsDistributor.sol (L197-208)
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
```

**File:** contracts/RewardsDistributor.sol (L248-254)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
```

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/RewardsDistributor.sol (L271-280)
```text
    function _updateTokenIndex(IERC20 token_) private {
        TokenState storage _supplyState = tokenStates[token_];
        (uint224 _newIndex, uint32 _newTimestamp) = _calculateTokenIndex(_supplyState, token_);
        if (_newIndex > 0 && _newTimestamp > 0) {
            _supplyState.index = _newIndex;
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_newIndex, _newTimestamp);
        } else if (_newTimestamp > 0) {
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_supplyState.index, _newTimestamp);
```

**File:** contracts/DepositToken.sol (L121-130)
```text
     * @notice Update reward contracts' states
     * @dev Should be called before balance changes (i.e. mint/burn)
     */
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
```
