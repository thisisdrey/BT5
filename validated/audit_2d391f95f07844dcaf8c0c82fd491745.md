### Title
Uninitialized `accountIndexOf` over-accrues rewards when token index equals `INITIAL_INDEX`, draining the distributor - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
Analogous to CVE-2008-0062 (a global variable left unset for certain message types leading to a crash/exploit), `RewardsDistributor` fails to initialize `accountIndexOf[token][account]` for accounts that held a `DepositToken`/`DebtToken` balance *before* the reward token was registered. The fallback in `_calculateTokenDelta` uses a strict `>` comparison against `INITIAL_INDEX`, so when the supply index is exactly `INITIAL_INDEX` (freshly added token, or index unchanged because speed/timestamp produced no growth), the uninitialized account index of `0` is kept, producing a `_deltaIndex` of `1e18` and crediting the account `balanceOf(account)` reward tokens instantly. Any unprivileged user can trigger this via the permissionless `claimRewards` / `updateBeforeMintOrBurn` entry points.

### Finding Description
`accountIndexOf` is only written inside `_updateTokensAccruedOf` [1](#0-0) . For a holder whose index was never recorded, `_calculateTokenDelta` applies a fallback:

```solidity
// contracts/RewardsDistributor.sol:222-230
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
``` [2](#0-1) 

When `_updateTokenSpeed` registers a token it stores `TokenState({index: INITIAL_INDEX, timestamp: now})` — i.e. exactly `1e18` [3](#0-2) . Because the guard is `_tokenIndex > INITIAL_INDEX` rather than `>=`, an uninitialized account with `_tokenIndex == INITIAL_INDEX` keeps `_accountIndex == 0`, yielding `_deltaIndex = 1e18` and `_tokensDelta = balanceOf(account)`.

The claim path is fully permissionless: `claimRewards(accounts_, tokens_)` is `public` and iterates any attacker-chosen accounts/tokens [4](#0-3) , and `updateBeforeMintOrBurn` "may be called by anyone" [5](#0-4) . `_transferRewardIfEnoughTokens` then pays out as long as the distributor's `rewardToken` balance covers `amount_` [6](#0-5) . The same overflow happens whenever a holder's accrued index is still `INITIAL_INDEX` (e.g. token added but index never advanced because `totalSupply == 0` at accrual time, `_ratio` computed as 0 [7](#0-6) ).

This also applies to `DebtToken` holders, since `updateBeforeMintOrBurn` is invoked for debt tokens too via `updateRewardsBeforeMintOrBurn` [8](#0-7) .

### Impact Explanation
Theft of unclaimed yield / direct draining of the distributor's `rewardToken` balance. An attacker holding a large `DepositToken` or `DebtToken` balance when the reward token is registered (or whenever the supply index still equals `INITIAL_INDEX`) receives `wadMul(balance, 1e18) = balance` reward tokens — potentially the distributor's entire balance — instead of zero. The window is wide: any account that never had `accountIndexOf` recorded while `_tokenIndex == INITIAL_INDEX` qualifies, and the inflated `tokensAccruedOf` persists even after the index grows. Even if the distributor cannot pay immediately, the inflated `tokensAccruedOf` remains claimable later once funded, permanently stealing rewards meant for all users.

### Likelihood Explanation
- Preconditions: a reward token added via `updateTokenSpeed`/`updateTokenSpeeds`/`syncTokenSpeed` while some account holds a tracked-token balance with no recorded index — the normal case whenever rewards are enabled on an already-used pool (depositors exist before speeds are set).
- Attacker needs no privileges: deposit before reward registration (or just hold existing debt/deposit tokens), then call `claimRewards(ownAccount)` — a public function — at any moment while the index is still `INITIAL_INDEX`, or claim later since the inflated accrual is stored.
- The same bug can also be triggered inadvertently (no attacker needed), which increases the chance the distributor is drained by honest users.
- Uncertainty: whether reward speeds have ever been enabled on a deployed pool with pre-existing holders and a funded distributor; the code path itself is live production code, not mocked.

### Recommendation
Change the fallback comparison to `>=`:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Alternatively, treat `_accountIndex == 0` as "use the current supply index" (no delta) unconditionally, matching Compound's `compInitialIndex` handling, and add a regression test asserting that a pre-existing holder earns zero rewards immediately after a token's speed is set.

### Proof of Concept
Hardhat test sketch (existing repo test setup, forking or fixture deployment):

```typescript
it("drains rewards via uninitialized accountIndex at INITIAL_INDEX", async () => {
  // fixture: pool, depositToken (e.g. msVAUSDC), rewardsDistributor, rewardToken funded
  const { pool, depositToken, distributor, rewardToken, alice } = await loadFixture(fixture);

  // 1. Alice deposits collateral BEFORE reward speed is set → balanceOf > 0, accountIndexOf == 0
  await depositToken.connect(alice).deposit(parseEther("1000"), alice.address);

  // 2. Governor enables rewards for the deposit token (index set to INITIAL_INDEX = 1e18)
  await distributor.connect(governor).updateTokenSpeed(depositToken.address, parseEther("1"));

  // 3. Same block / before any index growth: Alice claims
  await rewardToken.mint(distributor.address, parseEther("100000")); // distributor funded
  const before = await rewardToken.balanceOf(alice.address);
  await distributor.claimRewards(alice.address); // permissionless

  // 4. Alice receives ~balance * 1e18 wad = 1000 reward tokens despite zero elapsed accrual
  expect(await rewardToken.balanceOf(alice.address)).to.be.closeTo(
    before.add(parseEther("1000")), parseEther("1"));
});
```

Key assertion: `tokensAccruedOf[alice] == balanceOf(alice)` after a single `claimRewards`, demonstrating the uninitialized-index over-accrual.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-167)
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
```

**File:** contracts/RewardsDistributor.sol (L173-180)
```text
     * This function also may be called by anyone to update stored indexes
     */
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L203-208)
```text
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
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

**File:** contracts/RewardsDistributor.sol (L263-263)
```text
        accountIndexOf[token_][account_] = _tokenIndex;
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```

**File:** contracts/DebtToken.sol (L114-121)
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
