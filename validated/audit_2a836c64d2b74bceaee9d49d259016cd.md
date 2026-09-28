### Title
Mistakenly transferred tokens and native-token refunds get permanently stuck in `ProxyOFT` — ([File: contracts/ProxyOFT.sol](contracts/ProxyOFT.sol))

### Summary
`ProxyOFT` is the LayerZero bridge endpoint for synthetic tokens (e.g. msUSD, msETH). It burns/mints the `syntheticToken` rather than custodying it, and it receives native tokens both via `msg.value` on `sendFrom` and via LZ fee refunds. Unlike `DepositToken`, `DebtToken`, `NativeTokenGateway`, and `VesperGateway`, `ProxyOFT` does not inherit `TokenHolder` and exposes no `sweep`/withdrawal function. Any ERC20 (including the synthetic token itself) or ETH sent to it — whether by user mistake or as a fee refund — is permanently locked.

### Finding Description
- `ProxyOFT` inherits `SynthContext`, `ComposableOFTCoreUpgradeable`, `ProxyOFTStorageV1` — none provide token recovery. `TokenHolder.sweep` exists in `contracts/utils/TokenHolder.sol:37` and is used by other protocol contracts, but not here.
- `_debitFrom` burns the synthetic token and `_creditTo` mints it (`contracts/ProxyOFT.sol:81,91`), so a user who directly `transfer()`s msUSD to the `ProxyOFT` address (a common mistake: pasting the OFT adapter address instead of calling `sendFrom`, or sending the token "back to the bridge") loses it forever — the balance is never debited or credited.
- `sendFrom` is `payable` and takes `_refundAddress: payable(from_)` (`contracts/ProxyOFT.sol:115-128`); excess LZ fees are refunded to the user, but ETH pushed to the contract by other means (selfdestruct, refund to the contract address, mistaken `deposit`-style calls) cannot be withdrawn — there is no payable fallback in `TokenHolder` terms and no ETH withdrawal function.
- The same applies to `RewardsDistributor` and `MetAirdrop`, which hold reward/esMET token balances for claims and also lack `TokenHolder.sweep` — any foreign token sent there is stuck — but `ProxyOFT` is the stronger analog since users interact with it directly for bridging and it accumulates native-token value.

### Impact Explanation
Permanent freezing of funds: ERC20 tokens (including the protocol's own synthetic tokens) and ETH transferred to `ProxyOFT` are irrecoverable. This is identical in class to the Reservoir finding — a contract that receives tokens but offers no withdrawal path for assets it does not manage.

### Likelihood Explanation
Medium. It requires a user error (sending tokens to the adapter address rather than calling `sendFrom`), which is a well-documented real-world occurrence, especially since `ProxyOFT.sendFrom` is the expected entry point and users frequently `transfer()` to contract addresses by mistake. ETH can also accumulate via LayerZero refunds. However, loss is borne by the mistaken sender, not the protocol or third parties, matching the severity profile of the original M07 finding.

### Recommendation
Inherit `TokenHolder` in `ProxyOFT` and implement `_requireCanSweep` gated to the governor (`syntheticToken.poolRegistry().governor()`, already used as `owner()`), optionally blocking sweeps of `syntheticToken` balances that correspond to in-flight bridging if ever custodyed. Same for `RewardsDistributor`/`MetAirdrop` for non-reward tokens.

### Proof of Concept
Hardhat (fork or local, using existing mocks):

```ts
// Deploy PoolRegistry + SyntheticToken + ProxyOFT via existing test fixtures.
const [user] = await ethers.getSigners();

// User mistakenly sends msUSD directly to the ProxyOFT address
await msUSD.connect(user).transfer(proxyOFT.address, parseEther("100"));
expect(await msUSD.balanceOf(proxyOFT.address)).to.eq(parseEther("100"));

// No recovery path exists:
// - ProxyOFT has no sweep()/withdraw function (not a TokenHolder).
// - _debitFrom only burns via sendFrom flow; _creditTo only mints on LZ receive.
// Any call attempting to move those tokens reverts — they are permanently stuck.

// ETH path: force ETH into ProxyOFT via selfdestruct or LZ refund to contract;
// no payable withdrawal exists -> ETH locked permanently.
```

Note: certainty is limited — I could not fully enumerate `RecurringAirdrop.sol` internals or confirm every deployed ABI lacks a sweep variant, but the source of `ProxyOFT.sol` clearly shows no recovery function, and `TokenHolder` is deliberately present on sibling contracts, making its absence here a confirmed gap.