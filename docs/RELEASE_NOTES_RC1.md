# ScoreSight Release Candidate Notes (RC1)

Status: Draft (internal review)
Target release: TBD
Branch: `release/next-rc1`
Prepared by: ArieR1963 + contributors
Date: 2026-02-23

## 1. Scope of This RC

This release candidate bundles recent improvements in OCR workflow, UI/stability, and output integrations, plus a Tesseract runtime update.

Goals for RC1:
- Technically validate on Windows, macOS (Intel + Apple Silicon), and Linux
- Detect functional regressions early
- Measure OCR quality on real scoreboard cases

## 2. Key Changes

Please review and refine this section for relevance:

- Improved OCR workflow and field mapping
- Improvements in vMix / vMix API+ output flow
- UI/translation updates across multiple languages
- OCR Training Dojo workflow updates
- Tesseract runtime update: `5.5.1 -> 5.5.2`

## 3. Technical Update: Tesseract

- Python bridge: `tesserocr 2.10.0`
- Linked Tesseract runtime: `5.5.2`
- Local smoke validation completed:
  - App startup smoke test: passed
  - Basic OCR recognition test: passed

Note:
- Full retraining is not required for existing `.traineddata` models for this patch update within Tesseract 5.x.

## 4. Test Status (to be completed)

### 4.1 Platforms

- Windows: pending validation
- macOS Intel: pending validation
- macOS Apple Silicon (M1-M4): pending validation
- Linux: pending validation

### 4.2 Functional Checks

- Source selection / capture: pending validation
- OCR detection on live scoreboards: pending validation
- Output to vMix / API / CSV / JSON / XML: pending validation
- Long-run performance: pending validation

## 5. Known Risks / Considerations

- Historical annotated training dataset is not currently available
- New model improvements require (re-)annotation based on available videos
- Cross-platform packaging still requires full validation

## 6. What Testers Should Explicitly Evaluate

- OCR accuracy by scoreboard type
- Stability during longer sessions
- UI regressions and translation issues
- Output consistency (vMix/API) without delay or missed updates

## 7. Feedback Questions for RC1

1. Which use cases are measurably better than the previous release?
2. Which regressions block a public release?
3. Which platforms are production-ready?
4. Which improvements should move to RC2?

## 8. Release Go/No-Go Checklist

- [ ] Core flows tested on all target platforms
- [ ] No blocker bugs open
- [ ] Release notes content finalized
- [ ] Build artifacts available for all platforms
- [ ] PR ready with clear test evidence and changelog
