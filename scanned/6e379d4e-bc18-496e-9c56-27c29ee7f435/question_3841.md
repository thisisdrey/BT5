# Q3841: Smart account deployment and initialization: init arrays / init replay / one-shot configure

## Question
Can an unprivileged deployer or external caller with no guardian or admin powers enter through `SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)` with delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer while the factory is deploying a brand-new Safe through the standard proxy path and make `configureSmartAccount` run more than once or run in a context that was not the intended fresh Safe, breaking the rule that `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe and leading to Bypass of intended Safe initialization or module-setup permissions?

## Target
- File/function: contracts/SmartAccountFactory.sol / deploySmartAccount, configureSmartAccount, predictSmartAccountAddress
- Entrypoint: SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)
- Attacker controls: delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer
- Exploit idea: make `configureSmartAccount` run more than once or run in a context that was not the intended fresh Safe
- Invariant to test: `configureSmartAccount` should run at most once and only in the intended delegatecall context of a fresh Safe
- Expected Immunefi impact: Bypass of intended Safe initialization or module-setup permissions
- Fast validation: Forge test repeated delegatecall attempts into `configureSmartAccount` and assert the one-shot guard holds in every reachable context.
