# Implementation Plan: Clonagem de Endpoints MPV2 a partir de Template

## Overview

Substituir a criação hardcoded de endpoints HLS/DASH por clonagem a partir do canal MPV2 do template MediaLive. Implementação incremental: funções puras primeiro, integração depois, fallback preservado.

## Tasks

- [ ] 1. Implement `_detect_template_mpv2_channel()`
  - [x] 1.1 Create the `_detect_template_mpv2_channel()` function in `lambdas/configuradora/handler.py`
    - Add the `_MPV2_INGEST_URL_PATTERN` regex constant
    - Implement dual detection: `MediaPackageSettings` format first, then CMAF URL parsing
    - Return `(channel_group, channel_name)` tuple or `None`
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3_

  - [ ] 1.2 Write property tests for `_detect_template_mpv2_channel()`
    - Test that MediaPackageSettings format always extracts correct ChannelGroup/ChannelName
    - Test that CMAF URL format always extracts correct ChannelGroup/ChannelName via regex
    - Test that unrecognized formats return `None`
    - Use `@settings(max_examples=10)` and Hypothesis strategies
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3_

  - [ ] 1.3 Write unit tests for `_detect_template_mpv2_channel()` edge cases
    - Test with real `AWS_LL_CHANNEL.json` destination format (MediaPackageSettings)
    - Test with real `0001_WARNER_CHANNEL` destination format (CMAF URL)
    - Test with empty `MediaPackageSettings` list falling through to URL detection
    - Test with no destinations returning `None`
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3_

- [ ] 2. Implement `_generate_cloned_endpoint_name()`
  - [x] 2.1 Create the `_generate_cloned_endpoint_name()` function in `lambdas/configuradora/handler.py`
    - Standard channels (no "LL" in name): detect manifest type → suffix `_HLS` or `_DASH`
    - Low-Latency channels ("LL" in name): detect `CmafEncryptionMethod` → suffix `_CBCS` or `_CENC`
    - Return `{new_channel_name}{suffix}`
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

  - [ ] 2.2 Write property tests for `_generate_cloned_endpoint_name()`
    - Standard channel names (no "LL") always produce `_HLS` or `_DASH` suffix
    - Low-Latency channel names (with "LL") always produce `_CBCS` or `_CENC` suffix
    - Output always starts with `new_channel_name`
    - Use `@settings(max_examples=10)` and Hypothesis strategies
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

  - [ ] 2.3 Write unit tests for `_generate_cloned_endpoint_name()` edge cases
    - Test `0001_WARNER_CHANNEL_HLS` → `NOVO_CANAL_HLS` (standard HLS)
    - Test `0001_WARNER_CHANNEL_DASH` → `NOVO_CANAL_DASH` (standard DASH)
    - Test `0008_BAND_NEWS_LL_CBCS` → `NOVO_CANAL_LL_CBCS` (LL with CBCS/FAIRPLAY)
    - Test `0008_BAND_NEWS_LL_CENC` → `NOVO_CANAL_LL_CENC` (LL with CENC/PLAYREADY+WIDEVINE)
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

- [ ] 3. Implement `_clone_endpoint_config()`
  - [x] 3.1 Create the `_ENDPOINT_READONLY_FIELDS` constant and `_clone_endpoint_config()` function in `lambdas/configuradora/handler.py`
    - Deep copy via `copy.deepcopy()`
    - Remove read-only fields: `Arn`, `CreatedAt`, `ModifiedAt`, `ETag`, `Tags`
    - Remove `Url` from each manifest entry in `HlsManifests`, `LowLatencyHlsManifests`, `DashManifests`
    - Substitute `ChannelGroupName`, `ChannelName`, `OriginEndpointName`
    - Substitute `SpekeKeyProvider.ResourceId`, `RoleArn`, `Url` from env vars
    - Call `_generate_cloned_endpoint_name()` for the new endpoint name
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.1, 7.2, 7.3_

  - [ ] 3.2 Write property tests for `_clone_endpoint_config()`
    - Cloned config never contains read-only fields (`Arn`, `CreatedAt`, `ModifiedAt`, `ETag`, `Tags`)
    - Cloned config always has correct `ChannelGroupName`, `ChannelName`
    - `SpekeKeyProvider.ResourceId` always matches the provided `drm_resource_id`
    - Manifest URLs are always removed from cloned config
    - Use `@settings(max_examples=10)` and Hypothesis strategies
    - _Requirements: 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 7.1, 7.2_

  - [ ] 3.3 Write unit tests for `_clone_endpoint_config()` edge cases
    - Test with a full LL endpoint config (LowLatencyHlsManifests + CBCS encryption)
    - Test with a standard HLS endpoint config (HlsManifests + CBCS encryption)
    - Test with a DASH endpoint config (DashManifests + CENC encryption)
    - Test that original template config is not mutated (deep copy verification)
    - Test endpoint without encryption section (no SpekeKeyProvider)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 7.1, 7.2, 7.3_

- [x] 4. Checkpoint — Verify pure functions
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Implement `_fetch_template_endpoints()` and `_apply_cdn_auth_policy()`
  - [x] 5.1 Create the `_fetch_template_endpoints()` function in `lambdas/configuradora/handler.py`
    - Call `mediapackagev2_client.list_origin_endpoints()` then `get_origin_endpoint()` for each
    - Return list of full endpoint configs; return empty list on `ClientError`
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x] 5.2 Refactor CDN auth logic into `_apply_cdn_auth_policy()` in `lambdas/configuradora/handler.py`
    - Extract the inline CDN auth policy code from `_execute_orchestrated_creation()` Step 2
    - Create `_apply_cdn_auth_policy(params, endpoint_name)` function
    - Replace the inline code in the existing loop with a call to `_apply_cdn_auth_policy()`
    - _Requirements: 6.1, 6.2, 6.3_

  - [ ]* 5.3 Write unit tests for `_fetch_template_endpoints()`
    - Test successful fetch returns list of endpoint configs
    - Test `ClientError` on `list_origin_endpoints` returns empty list
    - Test `ClientError` on `get_origin_endpoint` returns empty list
    - Test channel with no endpoints returns empty list
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [ ]* 5.4 Write unit tests for `_apply_cdn_auth_policy()`
    - Test successful policy application
    - Test failure logs warning but does not raise
    - Test skipped when `CDN_SECRET_ARN` or `CDN_SECRET_ROLE_ARN` are empty
    - _Requirements: 6.1, 6.2, 6.3_

- [ ] 6. Modify `_execute_orchestrated_creation()` to use cloning with fallback
  - [x] 6.1 Update Step 2 in `_execute_orchestrated_creation()` in `lambdas/configuradora/handler.py`
    - Call `_detect_template_mpv2_channel(template_destinations)` to find template MPV2 channel
    - If detected, call `_fetch_template_endpoints()` to get template endpoint configs
    - If endpoints found, clone each via `_clone_endpoint_config()` and create via `create_resource()`
    - Register each cloned endpoint in `rollback_stack` with `RollbackEntry`
    - Apply CDN auth via `_apply_cdn_auth_policy()` for each cloned endpoint
    - If detection fails or no endpoints, fallback to existing `_build_endpoint_config()` loop
    - Log warning when fallback is activated with the reason
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 4.1, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2, 6.3, 7.1, 7.2, 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 9.3, 9.4, 9.5, 11.1, 11.2, 11.3_

  - [ ]* 6.2 Write unit tests for cloning integration in `_execute_orchestrated_creation()`
    - Test successful cloning from MediaPackageSettings template (2 endpoints cloned)
    - Test successful cloning from CMAF URL template (2 endpoints cloned)
    - Test fallback when `_detect_template_mpv2_channel()` returns `None`
    - Test fallback when `_fetch_template_endpoints()` returns empty list
    - Test rollback includes cloned endpoints on failure at Step 3 or 4
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 9.3, 9.4, 9.5, 11.1, 11.2, 11.3_

- [x] 7. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Pure functions (tasks 1-3) are implemented first for easy testing, then wired into the orchestration flow (tasks 5-6)
- `_build_endpoint_config` is preserved unchanged as fallback (Requirement 8.4)
- Property tests use `@settings(max_examples=10)` per project convention
- All tests go in `tests/` directory following existing naming patterns
