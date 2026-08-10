# Changelog

All notable changes are documented here. The format follows
[Conventional Commits](https://www.conventionalcommits.org/) and versions follow
[Semantic Versioning](https://semver.org/). Entries are generated automatically by
[release-please](https://github.com/googleapis/release-please).

## [0.8.0](https://github.com/cyberlabrs/andro-cd/compare/v0.7.0...v0.8.0) (2026-08-10)


### Features

* **e2e:** bootstrap + teardown scripts for zero-config sandbox setup ([214f39d](https://github.com/cyberlabrs/andro-cd/commit/214f39d0c91d2d4eeebb01681fa95cdb5f23571d))
* surface DB persistence status + safe delete for stale orphans ([9a3984d](https://github.com/cyberlabrs/andro-cd/commit/9a3984d10ddcdead27baf63a0f7e792af8467724))


### Bug Fixes

* **e2e:** kit shakedown — bash 3.2 compat, IAM trust, run.sh auth, block-list manifests ([ea53ed7](https://github.com/cyberlabrs/andro-cd/commit/ea53ed7e7b26586db747d1a7193edfd2efa7a04b))
* **e2e:** replace mapfile with while-read for macOS bash 3.2 compat ([b760ea6](https://github.com/cyberlabrs/andro-cd/commit/b760ea6080ec7e646bb5c33c552ca960adea9615))
* **e2e:** send bootstrap log helpers to stderr so ARN captures aren't polluted ([7a7a850](https://github.com/cyberlabrs/andro-cd/commit/7a7a850932e40b7e3a150bad2b808afd87d51412))
* templating on raw YAML text + PortMapping.name/appProtocol pass-through ([b438981](https://github.com/cyberlabrs/andro-cd/commit/b4389815a9ac7ffd9dd7102311d4a527197da9e7))

## [0.7.0](https://github.com/cyberlabrs/andro-cd/compare/v0.6.0...v0.7.0) (2026-08-08)


### Features

* **ui:** deployment strategy panel with live bake-time countdown ([8468dba](https://github.com/cyberlabrs/andro-cd/commit/8468dba804ce7f886551885b39e8f5b8049b336e))

## [0.6.0](https://github.com/cyberlabrs/andro-cd/compare/v0.5.0...v0.6.0) (2026-08-08)


### Features

* ECS native blue/green, canary and linear deployment strategies ([ac5dab9](https://github.com/cyberlabrs/andro-cd/commit/ac5dab98708538317b9f5ad7005df29a340640bc))

## [0.5.0](https://github.com/cyberlabrs/andro-cd/compare/v0.4.0...v0.5.0) (2026-08-08)


### Features

* **ui:** richer side-by-side diff — YAML default, syntax highlight, line numbers, fold ([e481762](https://github.com/cyberlabrs/andro-cd/commit/e481762fe1e88df90f31f064acd6221ac26bde57))

## [0.4.0](https://github.com/cyberlabrs/andro-cd/compare/v0.3.1...v0.4.0) (2026-08-08)


### Features

* **auth:** sliding-window session refresh with absolute cap ([3f56895](https://github.com/cyberlabrs/andro-cd/commit/3f5689510bc32a0a6cd3866b637ee177e9409af4))
* EFS volumes, FireLens, Service Connect and ALB request autoscaling ([832aa96](https://github.com/cyberlabrs/andro-cd/commit/832aa9618f2fcf71c38dec5537ea835091c27174))

## [0.3.1](https://github.com/cyberlabrs/andro-cd/compare/v0.3.0...v0.3.1) (2026-07-23)


### Bug Fixes

* refresh repo on manual sync and widen log stream window ([a89ae2f](https://github.com/cyberlabrs/andro-cd/commit/a89ae2f6a8f29554f9c54fc9de3d834d3a487a20))

## [0.3.0](https://github.com/cyberlabrs/andro-cd/compare/v0.2.0...v0.3.0) (2026-07-23)


### Features

* add ECSTask kind for one-off tasks with run-now ([a22081c](https://github.com/cyberlabrs/andro-cd/commit/a22081cb90c4fedb55b9dcfb9eb883ddca4e3b0e))
* add generic OIDC login (AUTH_MODE=oidc) ([2f63f72](https://github.com/cyberlabrs/andro-cd/commit/2f63f725a81f80b37c283caffd7997bcf6f3a98c))
* deployment timeline in the History tab ([31fdf54](https://github.com/cyberlabrs/andro-cd/commit/31fdf542df025cff7871928c357662962cabfc3f))

## [0.2.0](https://github.com/cyberlabrs/andro-cd/compare/v0.1.0...v0.2.0) (2026-07-22)


### Features

* create ALB target groups and listener rules from the manifest ([a942655](https://github.com/cyberlabrs/andro-cd/commit/a942655c098993fb5043ba395007f03d2f985285))

## 0.1.0 (unreleased)

Initial public version: pull-based GitOps controller for AWS ECS with an Argo-style
dashboard — four manifest kinds (ECSService, ECSScheduledTask, ECSServiceSet,
ECSCluster), GitHub OAuth + RBAC + API tokens + audit log, HA leader election,
dry-run mode, values-file templating, sync windows, Fargate Spot capacity providers,
autoscaling, webhooks, Prometheus metrics and Slack notifications.
