### Title
Accrued rewards are permanently frozen for any account blacklisted by the reward token (no recipient parameter) - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor.claimRewards` always transfers `rewardToken` directly to the accruing `account_`, with no way to specify an alternative recipient. If the reward token (an upgradeable ERC20 such as USDC/USDT, or any token that later introduces a blacklist) blacklists an account that has accrued rewards, every `claimRewards` call for that account reverts inside `safeTransfer`, and the accrued `tokensAccruedOf[account]` balance can never be paid out. The same pattern exists in `RecurringAirdrop.claim`, which can only send to `msg.sender`. This mirrors the reported Derby `withdrawRewards()` issue where deposit/withdraw support a `recipient` but the reward path does not.

### Finding Description
All three `claimRewards` overloads funnel into `claimRewards(address[] accounts_, IERC20[] tokens_)`, which calls `_transferRewardIfEnoughTokens(_account, tokensAccruedOf[_account])`. The payout is hardcoded to the account itself: [1](#0-0) [2](#0-1) 

```solidity
function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
    IERC20 _rewardToken = rewardToken;
    uint256 _balance = _rewardToken.balanceOf(address(this));
    if (amount_ > 0 && amount_ <= _balance) {
        tokensAccruedOf[account_] = 0;
        _rewardToken.safeTransfer(account_, amount_);   // always to account_
        emit RewardClaimed(account_, amount_);
    }
}
```

There is no `to_`/`recipient_` parameter anywhere in the claim path. Note the design already allows *anyone* to trigger a claim for any account (`claimRewards(address account_)` is permissionless), so rewards are force-pushed to the account — the account cannot even route them elsewhere.

`RecurringAirdrop.claim` has the same defect: the leaf encodes `msg.sender`, `claimed[msg.sender]` is incremented, and `_transferReward(msg.sender, _claimable)` sends the airdrop token to `msg.sender` only: [3](#0-2) [4](#0-3) 

Unlike Derby, `DepositToken`/`Pool` withdrawals do not offer a recipient escape hatch for rewards either — the accrued balance lives only in `tokensAccruedOf` and can only be released via a successful `rewardToken.transfer(account_)`.

### Impact Explanation
Permanent freezing of unclaimed yield: an account blacklisted by `rewardToken` after accruing rewards can never receive them; `safeTransfer` reverts on every attempt, so `tokensAccruedOf[account]` stays nonzero but unclaimable forever. The tokens remain locked in the `RewardsDistributor` (or `RecurringAirdrop`) contract. This is identical in impact to the reported MainVault issue, and is realistic because major tokens (USDC, USDT) are upgradeable and can add/extend blacklists after deployment, so the blacklist need not exist at deposit time.

### Likelihood Explanation
Requires the configured `rewardToken` (or airdrop `token`) to be or become a blacklisting token and to blacklist a user who holds accrued rewards — an external, low-probability but non-zero event, consistent with the original Medium rating. No attacker action is needed; the frozen state is permissionlessly triggerable since `claimRewards` can be called by anyone for the victim and will deterministically revert.

### Recommendation
Add a recipient parameter to the claim path, e.g. `claimRewards(address account_, IERC20[] tokens_, address to_)`, gated so that either `msg.sender == account_` or `msg.sender` is an authorized operator, and transfer to `to_`. For `RecurringAirdrop`, include the recipient in the leaf (`keccak256(abi.encodePacked(account, recipient, amount))`) or let the account set a claim recipient, so a blacklisted claimer can redirect funds.

### Proof of Concept
Hardhat/Foundry fork outline against the deployed mainnet `RewardsDistributor` (see `deployments/mainnet/RewardsDistributor.json`) or `MetRewardsDistributor`/`MetAirdrop` (`deployments/mainnet/MetAirdrop.json`):

1. User deposits into a pool so `updateBeforeMintOrBurn` accrues `tokensAccruedOf[user] > 0` (or is a valid leaf in `RecurringAirdrop` with `claimed[user] < amount`).
2. On a fork, use the reward token's owner/blacklister role (e.g. USDC `blacklist(user)` via `eth_call`/`hardhat_setStorageAt`, or a mock upgradeable token configured as `rewardToken`) to blacklist `user`.
3. Call `rewardsDistributor.claimRewards(user)` from any address → the call reaches `_transferRewardIfEnoughTokens` and reverts inside `rewardToken.safeTransfer(user, amount)`.
4. Assert `tokensAccruedOf[user]` is unchanged and `rewardToken.balanceOf(distributor)` still holds the funds — no parameter exists to redirect them. Repeat for `RecurringAirdrop.claim(amount, proof)` from `user` → reverts at `token.safeTransfer(msg.sender, _claimable)`.

A unit variant: deploy `RewardsDistributor` with a mock blocklistable ERC20 as `rewardToken`, accrue via `updateBeforeMintOrBurn`, blacklist the user, and show `claimRewards` always reverts.

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

**File:** contracts/utils/RecurringAirdrop.sol (L52-66)
```text
    function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
        if (merkleRoot == bytes32(0)) revert NothingToClaim();

        bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
        if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

        uint256 _claimable = amount_ - claimed[msg.sender];
        if (_claimable == 0) revert NothingToClaim();

        claimed[msg.sender] += _claimable;

        _transferReward(msg.sender, _claimable);

        emit RewardClaimed(msg.sender, _claimable);
    }
```

**File:** contracts/utils/RecurringAirdrop.sol (L73-75)
```text
    function _transferReward(address to_, uint256 amount_) internal virtual {
        token.safeTransfer(to_, amount_);
    }
```
