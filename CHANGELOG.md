# Changelog

All notable changes are documented here. The format follows
[Conventional Commits](https://www.conventionalcommits.org/) and versions follow
[Semantic Versioning](https://semver.org/). Entries are generated automatically by
[release-please](https://github.com/googleapis/release-please).

## [0.2.0](https://github.com/cyberlabrs/andro-cd/compare/v0.1.0...v0.2.0) (2026-07-22)


### Features

* create ALB target groups and listener rules from the manifest ([a942655](https://github.com/cyberlabrs/andro-cd/commit/a942655c098993fb5043ba395007f03d2f985285))

## 0.1.0 (unreleased)

Initial public version: pull-based GitOps controller for AWS ECS with an Argo-style
dashboard — four manifest kinds (ECSService, ECSScheduledTask, ECSServiceSet,
ECSCluster), GitHub OAuth + RBAC + API tokens + audit log, HA leader election,
dry-run mode, values-file templating, sync windows, Fargate Spot capacity providers,
autoscaling, webhooks, Prometheus metrics and Slack notifications.
