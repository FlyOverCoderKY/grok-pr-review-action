# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic versioning for stable releases.

## Unreleased

### Changed

- Operational review failures now fail the action independently of `fail_on`.
- Review artifacts are versioned and validated between composite-action steps.
- Grok reads an inert snapshot of the exact reviewed commit rather than mutable checkout state.
- GitHub operations are bounded by a configurable timeout and large review output is continued without dropping validated findings.
- Public `savage` and `diabolical` tones require an explicit governance opt-in.

### Security

- Missing or malformed Grok exit markers fail closed.
- Action inputs are validated before authentication, installation, or model execution.
