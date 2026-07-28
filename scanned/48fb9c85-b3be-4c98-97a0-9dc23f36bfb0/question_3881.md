# Q3881: Smart account deployment and initialization: init arrays / unexpected privilege / one-shot configure

## Question
Can an unprivileged deployer or external caller with no guardian or admin powers enter through `SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)` with delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer while other users or systems rely on the predicted address before the account is actually deployed and make duplicate or crafted initializer arrays install more delegate or allowance surface than intended, breaking the rule that `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe and leading to Bypass of intended Safe initialization or module-setup permissions?

## Target
- File/function: contracts/SmartAccountFactory.sol / deploySmartAccount, configureSmartAccount, predictSmartAccountAddress
- Entrypoint: SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)
- Attacker controls: delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer
- Exploit idea: make duplicate or crafted initializer arrays install more delegate or allowance surface than intended
- Invariant to test: `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe
- Expected Immunefi impact: Bypass of intended Safe initialization or module-setup permissions
- Fast validation: Forge test repeated delegatecall attempts into `configureSmartAccount` and assert the one-shot guard holds in every reachable context.
