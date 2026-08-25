# Verification summary

Generated from JUnit XML and current evidence files.

- Backend: 146/146 passed; failed=0; skipped=0.
- Frontend E2E: 13/13 passed; failed=0; final NAS run uses source revision b257f34ca3b42a6bbe32f0e9b370dd10cdf80020 and frontend image digest sha256:2992e4355db4f982a97cec5eca55f9818cd9cee3bc4d1e962aa686b41168cabd.
- Static checks: Ruff and frontend production build are recorded as machine results in this directory.
- NAS runtime, persistence, source binding, migration, image secret scan, PIT, and acceptance manifest are selected by the final reviewer bundle manifest.
- Current provider quota/error states remain explicit fail-closed states; no synthetic data is introduced.
