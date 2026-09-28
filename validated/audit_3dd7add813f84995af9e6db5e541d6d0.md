### Title
Unprivileged reward-index update permanently skips emissions while a rewarded token has zero supply - ([File: contracts/RewardsDistributor.sol])

### Summary
`RewardsDistributor._calculateTokenIndex()` advances a rewarded token's timestamp even when `token_.totalSupply() == 0`, but assigns zero reward index for the elapsed interval. Any account can force that checkpoint through the public `updateBeforeMintOrBurn()`, `updateBeforeTransfer()`, or `claimRewards()` functions, so emissions configured for an empty `DepositToken` or `DebtToken` interval are never credited to anyone. [1](#0-0) [2](#0-1) 

### Finding Description
Rewards are streamed according to `tokenSpeeds[token_]` and converted into a global index by dividing the elapsed emission amount by the rewarded token's total supply. When the total supply is zero, `_calculateTokenIndex()` computes `_ratio` as zero but still stores `block.timestamp` as the new checkpoint, permanently discarding `_deltaTimestamps * _speed` from all users' future accrual. [3](#0-2) 

The checkpoint is committed by `_updateTokenIndex()`, which unconditionally persists the unchanged index and updated timestamp returned for the zero-supply interval. [4](#0-3) 

`updateBeforeMintOrBurn()` is explicitly documented as callable by anyone and has no `onlyPool`, token-existence, pause, or privileged-sender restriction beyond requiring an initialized token state. [5](#0-4) 

The same checkpoint can also be triggered by public `claimRewards()` calls because that function updates the token index before processing account accruals. [6](#0-5) 

Once the timestamp advances, account rewards are calculated only from the delta between the unchanged global index and the account's index, so nobody receives the skipped interval's emissions. [7](#0-6) 

`RewardsDistributor` does not inherit `TokenHolder` or expose a recovery function for the undistributed `rewardToken` balance; its only outbound reward path is `_transferRewardIfEnoughTokens()` during claims. [8](#0-7) [9](#0-8) 

### Impact Explanation
Emissions intended for holders during an empty-supply interval become permanently unclaimable protocol inventory rather than being carried forward, redirected, or burned. If rewards are externally funded for a fixed campaign or the token's speed later returns to zero, those skipped rewards remain locked in `RewardsDistributor`, permanently freezing unclaimed yield and reducing the emissions available to legitimate later holders. [10](#0-9) [9](#0-8) 

### Likelihood Explanation
The attacker only needs a rewarded `DepositToken` or `DebtToken` to have zero total supply at the instant of an index checkpoint. A public caller can then invoke `RewardsDistributor.updateBeforeMintOrBurn(token, attackerOrAnyAccount)` or `claimRewards(accounts, tokens)`; the latter remains callable under `nonReentrant`, and the former has no sender restriction. [11](#0-10) 

Zero supply can occur naturally before first use or after the final withdrawal, and normal `DepositToken` mint/burn paths already update every registered rewards distributor before changing balances. [12](#0-11) 

The issue does not require manipulating the governor-set speed itself: it only requires `tokenSpeeds[token_] > 0`, an elapsed nonzero timestamp delta, and zero rewarded-token supply when the public checkpoint executes. [3](#0-2) 

### Recommendation
Do not advance `tokenStates[token_].timestamp` when `token_.totalSupply() == 0`, so emissions accruing during an empty interval are carried into the next non-empty interval. Alternatively, account for the skipped amount in a recoverable treasury balance or explicitly burn/route it, rather than silently erasing it from the accrual schedule. Add a governor-controlled reward-token recovery mechanism that cannot withdraw amounts already accrued in `tokensAccruedOf` or earned through live indexes. [1](#0-0) [9](#0-8) 

### Proof of Concept
```ts
// Hardhat fork test against deployed Pool, DepositToken, and RewardsDistributor.
import {expect} from 'chai'
import {ethers} from 'hardhat'
import {time} from '@nomicfoundation/hardhat-network-helpers'

it('burns an emissions interval while the rewarded token is empty', async () => {
  const [attacker, futureUser] = await ethers.getSigners()

  const depositToken = await ethers.getContractAt('IDepositToken', DEPOSIT_TOKEN)
  const distributor = await ethers.getContractAt('RewardsDistributor', DISTRIBUTOR)
  const rewardToken = await ethers.getContractAt('IERC20', await distributor.rewardToken())

  // Existing deployment state: token has nonzero speed and initialized state.
  const speed = await distributor.tokenSpeeds(depositToken.address)
  expect(speed).to.be.gt(0)
  expect(await depositToken.totalSupply()).to.eq(0)

  const before = await distributor.tokenStates(depositToken.address)
  const elapsed = 60 * 60
  await time.increase(elapsed)

  // Unprivileged public checkpoint. No governor, keeper, pool, or token role needed.
  await distributor
    .connect(attacker)
    .updateBeforeMintOrBurn(depositToken.address, attacker.address)

  const afterEmpty = await distributor.tokenStates(depositToken.address)
  expect(afterEmpty.index).to.eq(before.index)
  expect(afterEmpty.timestamp).to.be.gt(before.timestamp)

  // A later depositor cannot recover the skipped interval.
  await depositToken.connect(futureUser).deposit(DEPOSIT_AMOUNT, futureUser.address)
  await time.increase(1)
  await distributor.connect(attacker).updateBeforeMintOrBurn(
    depositToken.address,
    futureUser.address
  )

  const claimable = await distributor.claimable(futureUser.address)
  const expectedWithoutBug = speed.mul(elapsed + 1)
  expect(claimable).to.be.lt(expectedWithoutBug)

  // The skipped amount remains contract balance with no public recovery path.
  expect(await rewardToken.balanceOf(distributor.address)).to.be.gte(claimable)
})
```

The decisive assertion is that the zero-supply checkpoint changes `timestamp` without changing `index`; after supply returns, accrual starts from that new timestamp, so the elapsed empty interval can never enter any account's `tokensAccruedOf`. [1](#0-0) [13](#0-12)

### Citations

**File:** contracts/RewardsDistributor.sol (L39-49)
```text
contract RewardsDistributor is
    Initializable,
    ReentrancyGuardDeprecated,
    ReentrancyGuardTransient,
    Manageable,
    RewardsDistributorStorageV2
{
    using SafeERC20 for IERC20;
    using SafeCast for uint256;
    using WadRayMath for uint256;

```

**File:** contracts/RewardsDistributor.sol (L150-191)
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

**File:** contracts/RewardsDistributor.sol (L201-210)
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
```

**File:** contracts/RewardsDistributor.sol (L221-230)
```text
    ) private view returns (uint256 _tokenIndex, uint256 _tokensDelta) {
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

**File:** contracts/DepositToken.sol (L120-130)
```text
    /**
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
