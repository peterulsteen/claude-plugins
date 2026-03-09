# Changelog

## [1.2.0] - 2026-03-09

### Added
- Phase 8: Thorough code review — runs `/code-review:start` after all verification gates pass in Phase 7, ensuring a comprehensive multi-agent code review before raising a PR. Blocking findings trigger auto-fix cycles (max 3) before escalating to the user.
